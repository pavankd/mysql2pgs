"""Shared pieces for schema dumps: output paths, the file writer, jobs and progress."""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..models import DumpRequest

DUMP_ROOT = Path(os.environ.get("DUMP_ROOT", "/dumps"))
# How the dumps folder is known outside the container (shown in the UI).
DUMP_ROOT_DISPLAY = os.environ.get("DUMP_ROOT_DISPLAY", "./dumps")
MAX_LOG_ENTRIES = 5000


class DumpError(Exception):
    """A problem the user can fix (bad path, missing permission, tool failure)."""


class Cancelled(Exception):
    pass


def resolve_output(rel: str) -> Path:
    """Map a user-supplied relative path to a file inside DUMP_ROOT.

    Leading slashes are ignored (the path is always relative to the dumps folder), and
    anything that resolves outside it, including through symlinks, is rejected.
    """
    rel = rel.strip().replace("\\", "/")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise DumpError(f"Use a path relative to the dumps folder ({DUMP_ROOT_DISPLAY}), e.g. shop/shop.sql")
    if not rel or rel.endswith("/"):
        raise DumpError("Enter a file name for the dump, e.g. shop/shop-2026-11-09.sql")
    root = DUMP_ROOT.resolve()
    target = (root / rel).resolve()
    if target == root or not target.is_relative_to(root):
        raise DumpError(f"The dump must be written inside the dumps folder ({DUMP_ROOT_DISPLAY})")
    if target.name.endswith(".partial"):
        raise DumpError("File names ending in .partial are reserved for dumps in progress")
    return target


def display_path(p: Path) -> str:
    return f"{DUMP_ROOT_DISPLAY.rstrip('/')}/{p.relative_to(DUMP_ROOT.resolve()).as_posix()}"


class DumpWriter:
    """Writes to ``<file>.partial`` and renames to the final name only on success,
    so a failed or cancelled dump never leaves a truncated file behind."""

    def __init__(self, target: Path, job: "DumpJob"):
        self.target = target
        self.partial = target.with_name(target.name + ".partial")
        self.job = job
        target.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.partial, "wb")

    def write(self, data: str | bytes) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._fh.write(data)
        self.job.bytes_written += len(data)

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def commit(self) -> int:
        self._fh.close()
        os.replace(self.partial, self.target)
        return self.target.stat().st_size

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            self.partial.unlink(missing_ok=True)


@dataclass
class DumpJob:
    request: DumpRequest
    target: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"  # pending | running | completed | failed | cancelled
    phase: str = "queued"
    current: str = ""
    items_done: int = 0
    items_total: int = 0
    rows: int = 0
    bytes_written: int = 0
    file_size: int | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    warning_count: int = 0
    logs: list[dict] = field(default_factory=list)
    _seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def log(self, level: str, message: str) -> None:
        with self.lock:
            self._seq += 1
            if level == "warning":
                self.warning_count += 1
            self.logs.append({"seq": self._seq, "ts": time.time(), "level": level, "message": message})
            if len(self.logs) > MAX_LOG_ENTRIES:
                del self.logs[: len(self.logs) - MAX_LOG_ENTRIES]

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def snapshot(self, log_after: int = 0) -> dict:
        req = self.request
        with self.lock:
            logs = [e for e in self.logs if e["seq"] > log_after]
        return {
            "id": self.id,
            "status": self.status,
            "phase": self.phase,
            "current": self.current,
            "items_done": self.items_done,
            "items_total": self.items_total,
            "rows": self.rows,
            "bytes_written": self.bytes_written,
            "file_size": self.file_size,
            "output_path": display_path(self.target),
            "engine": req.connection.engine,
            "schemas": req.schemas,
            "content": req.content,
            "include_create_schema": req.include_create_schema,
            "error": self.error,
            "warning_count": self.warning_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": logs,
        }


class DumpJobStore:
    def __init__(self):
        self._jobs: dict[str, DumpJob] = {}
        self._lock = threading.Lock()
        self._busy_targets: set[Path] = set()

    def start(self, job: DumpJob, runner) -> DumpJob:
        with self._lock:
            if job.target in self._busy_targets:
                raise DumpError("Another dump is already writing to this file")
            self._busy_targets.add(job.target)
            self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job, runner), name=f"dump-{job.id}", daemon=True).start()
        return job

    def _run(self, job: DumpJob, runner) -> None:
        job.started_at = time.time()
        job.status = "running"
        writer: DumpWriter | None = None
        try:
            writer = DumpWriter(job.target, job)
            runner(job, writer)
            job.check_cancel()
            job.phase = "finishing"
            job.file_size = writer.commit()
            writer = None
            job.status = "completed"
            job.log("info", f"Dump written to {display_path(job.target)} ({job.file_size:,} bytes)")
        except Cancelled:
            job.status = "cancelled"
            job.log("warning", "Dump cancelled; no file was written")
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
            job.log("error", f"Dump failed: {job.error}")
            if not isinstance(e, DumpError):
                job.log("debug", traceback.format_exc())
        finally:
            if writer is not None:
                writer.abort()
            job.phase = "finished"
            job.finished_at = time.time()
            with self._lock:
                self._busy_targets.discard(job.target)

    def get(self, job_id: str) -> DumpJob | None:
        return self._jobs.get(job_id)


dump_jobs = DumpJobStore()


def sql_comment_header(writer: DumpWriter, engine_label: str, version: str, job: DumpJob, comment: str = "--") -> None:
    req = job.request
    content = {"schema": "schema only", "data": "data only", "both": "schema and data"}[req.content]
    writer.line(f"{comment} mysql2pgs schema dump")
    writer.line(f"{comment} Server : {engine_label} {version} ({req.connection.host}:{req.connection.port})")
    writer.line(f"{comment} Schemas: {', '.join(req.schemas)}")
    writer.line(f"{comment} Content: {content}; CREATE SCHEMA statements: {'yes' if req.include_create_schema else 'no'}")
    writer.line(f"{comment} Created: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    writer.line()
