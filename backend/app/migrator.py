"""Background migration jobs: in-memory state, progress and the execution loop."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass

import psycopg

from .db import mysql_connect, pg_connect
from .introspect import introspect
from .models import MigrationRequest
from .planner import Plan, Step, TablePlan, build_plan, pg_ident
from .typemap import mq, qi

MAX_LOG_ENTRIES = 5000


class Cancelled(Exception):
    pass


@dataclass
class TableProgress:
    key: str
    database: str
    source_table: str
    target: str
    status: str = "pending"  # pending | creating | copying | indexing | done | failed
    row_estimate: int = 0
    rows_copied: int = 0
    source_rows: int | None = None
    target_rows: int | None = None
    error: str | None = None
    seconds: float = 0.0


class Job:
    def __init__(self, request: MigrationRequest):
        self.id = uuid.uuid4().hex[:12]
        self.request = request
        self.status = "pending"  # pending | running | completed | completed_with_errors | failed | cancelled
        self.phase = "queued"
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.error: str | None = None
        self.tables: dict[str, TableProgress] = {}
        self.logs: list[dict] = []
        self._log_seq = 0
        self.warning_count = 0
        self.error_count = 0
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()

    # -- logging ---------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        with self.lock:
            self._log_seq += 1
            if level == "warning":
                self.warning_count += 1
            elif level == "error":
                self.error_count += 1
            self.logs.append({"seq": self._log_seq, "ts": time.time(), "level": level, "message": message})
            if len(self.logs) > MAX_LOG_ENTRIES:
                del self.logs[: len(self.logs) - MAX_LOG_ENTRIES]

    def snapshot(self, log_after: int = 0) -> dict:
        with self.lock:
            tables = [asdict(t) for t in self.tables.values()]
            logs = [entry for entry in self.logs if entry["seq"] > log_after]
            est = sum(max(t["row_estimate"], t["rows_copied"]) for t in tables)
            copied = sum(t["rows_copied"] for t in tables)
            return {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "warning_count": self.warning_count,
                "error_count": self.error_count,
                "rows_copied": copied,
                "rows_estimated": est,
                "tables_total": len(tables),
                "tables_done": sum(t["status"] == "done" for t in tables),
                "tables_failed": sum(t["status"] == "failed" for t in tables),
                "databases": [d.name for d in self.request.databases],
                "tables": tables,
                "logs": logs,
            }

    def summary(self) -> dict:
        s = self.snapshot(log_after=10**12)
        s.pop("tables")
        s.pop("logs")
        return s

    # -- helpers ---------------------------------------------------------
    def set_table(self, key: str, **fields) -> None:
        with self.lock:
            t = self.tables[key]
            for k, v in fields.items():
                setattr(t, k, v)

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, request: MigrationRequest) -> Job:
        job = Job(request)
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=run_job, args=(job,), name=f"migration-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)


jobs = JobStore()


# ---------------------------------------------------------------------------
# Planning (shared by preview and execution)
# ---------------------------------------------------------------------------

def make_plan(req: MigrationRequest) -> Plan:
    conn = mysql_connect(req.source)
    try:
        dbs = []
        for sel in req.databases:
            info = introspect(conn, sel.name, set(sel.tables) if sel.tables is not None else None)
            schema = (sel.target_schema or "").strip() or pg_ident(sel.name, req.options)
            dbs.append((info, schema))
    finally:
        conn.close()
    return build_plan(dbs, req.options)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _exec_step(pg: psycopg.Connection, job: Job, step: Step, level_on_fail: str = "warning") -> bool:
    try:
        pg.execute(step.sql)
        return True
    except psycopg.Error as e:
        if step.fallback_sql:
            try:
                pg.execute(step.fallback_sql)
                job.log("info", f"{step.label}: used fallback ({_err(e)})")
                return True
            except psycopg.Error as e2:
                e = e2
        job.log(level_on_fail, f"{step.label} failed: {_err(e)}")
        return False


def _err(e: Exception) -> str:
    msg = str(e).strip().splitlines()
    return msg[0] if msg else e.__class__.__name__


def _copy_table(job: Job, tp: TablePlan, pg: psycopg.Connection) -> None:
    req = job.request
    batch_size = req.options.batch_size
    converters = list(enumerate(tp.converters))
    active = [(i, c) for i, c in converters if c is not None]
    copied = 0
    last_report = 0.0

    my = mysql_connect(req.source, streaming=True)
    try:
        cur = my.cursor()
        cur.execute(tp.select_sql)
        copy_sql = f"COPY {tp.qualified} ({', '.join(qi(c) for c in tp.column_names)}) FROM STDIN"
        with pg.transaction(), pg.cursor() as pcur, pcur.copy(copy_sql) as copy:
            while True:
                job.check_cancel()
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    if active:
                        row = list(row)
                        for i, conv in active:
                            row[i] = conv(row[i])
                    copy.write_row(row)
                copied += len(rows)
                now = time.monotonic()
                if now - last_report > 0.25:
                    job.set_table(tp.key, rows_copied=copied)
                    last_report = now
        cur.close()
    finally:
        my.close()
    job.set_table(tp.key, rows_copied=copied)


def run_job(job: Job) -> None:
    req = job.request
    opts = req.options
    job.started_at = time.time()
    job.status = "running"
    try:
        job.phase = "introspecting"
        job.log("info", f"Reading schema metadata for: {', '.join(d.name for d in req.databases)}")
        plan = make_plan(req)
        with job.lock:
            for tp in plan.tables:
                job.tables[tp.key] = TableProgress(
                    key=tp.key, database=tp.database, source_table=tp.source_table,
                    target=f"{tp.schema}.{tp.name}", row_estimate=tp.row_estimate,
                )
        job.log("info", f"Plan ready: {len(plan.tables)} table(s), {len(plan.fk_steps)} foreign key(s), {len(plan.view_steps)} view(s)")
        for w in plan.warnings:
            job.log("warning", w)

        with pg_connect(req.target) as pg:
            job.phase = "creating schemas"
            for step in plan.schema_steps:
                job.check_cancel()
                if not _exec_step(pg, job, step, level_on_fail="error"):
                    raise RuntimeError(f"Could not prepare target schema ({step.label})")
                job.log("info", step.label)

            job.phase = "creating tables"
            live: list[TablePlan] = []
            for tp in plan.tables:
                job.check_cancel()
                job.set_table(tp.key, status="creating")
                try:
                    pg.execute(tp.create_sql)
                    live.append(tp)
                    job.log("info", f"Created table {tp.schema}.{tp.name}")
                except psycopg.Error as e:
                    msg = _err(e)
                    if isinstance(e, psycopg.errors.DuplicateTable):
                        msg += " (enable 'Drop existing schemas' to replace it)"
                    job.set_table(tp.key, status="failed", error=msg)
                    job.log("error", f"Create table {tp.schema}.{tp.name} failed: {msg}")

            if opts.include_data:
                job.phase = "copying data"
                for tp in live[:]:
                    job.check_cancel()
                    job.set_table(tp.key, status="copying")
                    t0 = time.monotonic()
                    try:
                        _copy_table(job, tp, pg)
                        t = job.tables[tp.key]
                        job.log("info", f"Copied {t.rows_copied:,} row(s) into {tp.schema}.{tp.name} in {time.monotonic() - t0:.1f}s")
                    except Cancelled:
                        raise
                    except Exception as e:  # noqa: BLE001 - report and continue with the next table
                        live.remove(tp)
                        job.set_table(tp.key, status="failed", error=_err(e))
                        job.log("error", f"Copy into {tp.schema}.{tp.name} failed: {_err(e)}")
                    finally:
                        job.set_table(tp.key, seconds=round(time.monotonic() - t0, 2))

            job.phase = "building keys & indexes"
            for tp in live:
                job.check_cancel()
                job.set_table(tp.key, status="indexing")
                for step in tp.post_steps:
                    if _exec_step(pg, job, step):
                        job.log("info", step.label)

            if plan.fk_steps:
                job.phase = "adding foreign keys"
                for step in plan.fk_steps:
                    job.check_cancel()
                    if _exec_step(pg, job, step):
                        job.log("info", step.label)

            if plan.view_steps:
                job.phase = "creating views"
                pending = list(plan.view_steps)
                # Views can depend on each other; retry until no further progress.
                while pending:
                    job.check_cancel()
                    failed: list[tuple[Step, str]] = []
                    for step in pending:
                        try:
                            pg.execute(step.sql)
                            job.log("info", step.label)
                        except psycopg.Error as e:
                            failed.append((step, _err(e)))
                    if len(failed) == len(pending):
                        for step, msg in failed:
                            job.log("warning", f"{step.label} failed (rewrite manually): {msg}")
                        break
                    pending = [s for s, _ in failed]

            if opts.verify_row_counts and opts.include_data:
                job.phase = "verifying row counts"
                my = mysql_connect(req.source)
                try:
                    for tp in live:
                        job.check_cancel()
                        with my.cursor() as cur:
                            cur.execute(f"SELECT COUNT(*) AS n FROM {mq(tp.database)}.{mq(tp.source_table)}")
                            src = int(cur.fetchone()["n"])
                        dst = pg.execute(f"SELECT COUNT(*) FROM {tp.qualified}").fetchone()[0]
                        job.set_table(tp.key, source_rows=src, target_rows=dst)
                        if src != dst:
                            job.log("warning", f"Row count mismatch for {tp.key}: MySQL {src:,} vs PostgreSQL {dst:,}")
                finally:
                    my.close()

            for tp in live:
                job.set_table(tp.key, status="done")

        with job.lock:
            failed_tables = sum(t.status == "failed" for t in job.tables.values())
        job.status = "completed_with_errors" if failed_tables or job.error_count else "completed"
        job.log("info", f"Migration finished: {job.status.replace('_', ' ')}")
    except Cancelled:
        job.status = "cancelled"
        job.log("warning", "Migration cancelled by user")
    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.error = _err(e)
        job.log("error", f"Migration failed: {_err(e)}")
        job.log("debug", traceback.format_exc())
    finally:
        with job.lock:
            for t in job.tables.values():
                if t.status in ("creating", "copying", "indexing"):
                    t.status = "failed"
                    t.error = t.error or job.status
        job.phase = "finished"
        job.finished_at = time.time()
