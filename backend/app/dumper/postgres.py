"""PostgreSQL: permission check via has_*_privilege(); dump with pg_dump (plain SQL)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading

from ..db import pg_connect
from ..models import DumpConn, PGConn
from ..typemap import qi
from .base import DumpError, DumpJob, DumpWriter

PG_DUMP = shutil.which("pg_dump") or "pg_dump"
DATA_PROGRESS = re.compile(r'^pg_dump: dumping contents of table "(.+)"$')
SCHEMA_PROGRESS = re.compile(r'^pg_dump: creating TABLE "(.+)"$')
CREATE_SCHEMA = re.compile(rb"^(CREATE SCHEMA |ALTER SCHEMA .* OWNER TO )")


def _conn(c: DumpConn) -> PGConn:
    return PGConn(host=c.host, port=c.port, user=c.user, password=c.password, database=c.database.strip() or "postgres")


def inspect(c: DumpConn) -> dict:
    with pg_connect(_conn(c)) as conn:
        version, user, database = conn.execute(
            "SELECT current_setting('server_version'), current_user, current_database()"
        ).fetchone()
        rows = conn.execute(
            """
            SELECT n.nspname,
                   has_schema_privilege(n.oid, 'USAGE'),
                   count(c.oid) FILTER (WHERE c.relkind IN ('r', 'p')),
                   count(c.oid) FILTER (WHERE c.relkind IN ('v', 'm')),
                   count(c.oid) FILTER (WHERE c.relkind IN ('r', 'p', 'm', 'f')
                                          AND NOT has_table_privilege(c.oid, 'SELECT')),
                   count(c.oid) FILTER (WHERE c.relkind = 'S' AND NOT has_sequence_privilege(c.oid, 'SELECT')),
                   (SELECT count(*) FROM pg_proc p WHERE p.pronamespace = n.oid)
            FROM pg_namespace n
            LEFT JOIN pg_class c ON c.relnamespace = n.oid
            WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            GROUP BY n.oid, n.nspname
            ORDER BY n.nspname
            """
        ).fetchall()

    schemas = []
    for name, usage, tables, views, locked_out, seq_denied, routines in rows:
        schema_issues, data_issues = [], []
        if not usage:
            schema_issues.append("USAGE privilege on the schema is missing")
            data_issues.append("USAGE privilege on the schema is missing")
        if locked_out:
            # pg_dump locks every table it dumps (ACCESS SHARE needs SELECT), even schema-only.
            msg = f"SELECT privilege missing on {locked_out} table(s); pg_dump must lock them"
            schema_issues.append(msg)
            data_issues.append(msg)
        if seq_denied:
            data_issues.append(f"SELECT privilege missing on {seq_denied} sequence(s)")
        schemas.append({
            "name": name,
            "tables": tables,
            "views": views,
            "routines": routines,
            "can_dump_schema": not schema_issues,
            "can_dump_data": not data_issues,
            "schema_issues": schema_issues,
            "data_issues": data_issues,
            "notes": [],
        })
    return {
        "engine": "postgres",
        "version": version,
        "user": user,
        "database": database,
        "schemas": schemas,
        "notes": [f"Schemas of database \"{database}\". Dumped with pg_dump {_pg_dump_version()}."],
    }


def _pg_dump_version() -> str:
    try:
        out = subprocess.run([PG_DUMP, "--version"], capture_output=True, text=True, timeout=10).stdout
        return out.split()[-1] if out else "?"
    except OSError:
        return "(not installed)"


def dump(job: DumpJob, writer: DumpWriter, info: dict) -> None:
    req = job.request
    c = _conn(req.connection)
    selected = [s for s in info["schemas"] if s["name"] in set(req.schemas)]
    job.items_total = sum(s["tables"] for s in selected)

    args = [PG_DUMP, "--format=plain", "--verbose", "--no-password", "--encoding=UTF8"]
    for name in req.schemas:
        # Double quotes make the pattern literal (case, and characters like * ? .).
        args += ["--schema", '"' + name.replace('"', '""') + '"']
    if req.content == "schema":
        args.append("--schema-only")
    elif req.content == "data":
        args.append("--data-only")

    if req.content == "data" and req.include_create_schema:
        # A data-only dump has no DDL; create the schemas so the INSERT/COPY targets resolve.
        writer.line("-- Schemas (added by mysql2pgs because CREATE SCHEMA was requested)")
        for name in req.schemas:
            writer.line(f"CREATE SCHEMA IF NOT EXISTS {qi(name)};")
        writer.line()

    env = {
        **os.environ,
        "PGHOST": c.host,
        "PGPORT": str(c.port),
        "PGUSER": c.user,
        "PGPASSWORD": c.password,
        "PGDATABASE": c.database,
        "PGCONNECT_TIMEOUT": "10",
        "PGAPPNAME": "mysql2pgs-dump",
    }
    job.phase = "dumping"
    job.log("info", f"Running pg_dump for {', '.join(req.schemas)}")
    drop_create_schema = not req.include_create_schema and req.content != "data"
    progress = DATA_PROGRESS if req.content != "schema" else SCHEMA_PROGRESS

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    errors: list[str] = []

    def read_stderr():
        done: set[str] = set()
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            m = progress.match(line)
            if m:
                done.add(m.group(1))
                job.items_done = len(done)
                job.current = m.group(1)
            elif line.startswith("pg_dump: error:") or line.startswith("pg_dump: detail:"):
                errors.append(line.removeprefix("pg_dump: "))
            elif line.startswith("pg_dump: warning:"):
                job.log("warning", line.removeprefix("pg_dump: "))

    t = threading.Thread(target=read_stderr, daemon=True)
    t.start()
    try:
        for line in proc.stdout:
            if drop_create_schema and CREATE_SCHEMA.match(line):
                continue
            writer.write(line)
            if job.cancel_event.is_set():
                proc.kill()
                job.check_cancel()
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(timeout=5)
    if proc.returncode != 0:
        raise DumpError("pg_dump failed: " + ("; ".join(errors) or f"exit code {proc.returncode}"))
