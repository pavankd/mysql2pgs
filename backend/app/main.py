from __future__ import annotations

import os

import oracledb
import psycopg
import pymssql
import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .db import mysql_connect, mysql_version, pg_create_database, pg_probe
from .introspect import list_databases, list_tables
from .migrator import jobs, make_plan
from . import workbench
from .dumper import service as dump_service
from .dumper.base import DUMP_ROOT_DISPLAY, DumpError, dump_jobs
from .models import (
    DumpConn,
    DumpRequest,
    MigrationRequest,
    MySQLConn,
    PGConn,
    QueryRequest,
    TablesRequest,
    WorkbenchConn,
    WorkbenchQuery,
)
from .query import QueryCancelled, describe_error, run_query, running

app = FastAPI(title="mysql2pgs", version="1.0.0", description="Migrate MySQL 5.6 databases to PostgreSQL")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_ERRORS = (pymysql.Error, psycopg.Error, OSError)


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, pymysql.err.OperationalError) and len(e.args) >= 2:
        return HTTPException(400, detail=str(e.args[1]))
    return HTTPException(400, detail=str(e).strip().splitlines()[0] if str(e).strip() else repr(e))


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/mysql/test")
def test_mysql(conn: MySQLConn):
    try:
        version = mysql_version(conn)
    except DB_ERRORS as e:
        raise _fail(e)
    major_minor = ".".join(version.split(".")[:2])
    note = None if major_minor == "5.6" else f"Tool is designed for MySQL 5.6; server reports {version}."
    return {"ok": True, "version": version, "note": note}


@app.post("/api/postgres/test")
def test_postgres(conn: PGConn):
    """200 with ok=false + database_missing=true when only the target database is absent."""
    try:
        return pg_probe(conn)
    except DB_ERRORS as e:
        raise _fail(e)


@app.post("/api/postgres/create-database")
def create_postgres_database(conn: PGConn):
    name = conn.database.strip()
    if not name:
        raise HTTPException(400, detail="Database name is required")
    if len(name.encode()) > 63:
        raise HTTPException(400, detail="Database name must be at most 63 bytes")
    try:
        return pg_create_database(conn.model_copy(update={"database": name}))
    except DB_ERRORS as e:
        raise _fail(e)


@app.post("/api/postgres/query")
def query_postgres(req: QueryRequest):
    if not req.sql.strip():
        raise HTTPException(400, detail="Query is empty")
    try:
        return run_query(req)
    except QueryCancelled as e:
        raise HTTPException(409, detail=e.reason)
    except psycopg.Error as e:
        raise HTTPException(400, detail=describe_error(e))
    except DB_ERRORS as e:
        raise _fail(e)


@app.post("/api/mysql/databases")
def databases(conn: MySQLConn):
    try:
        c = mysql_connect(conn)
        try:
            return list_databases(c)
        finally:
            c.close()
    except DB_ERRORS as e:
        raise _fail(e)


@app.post("/api/mysql/tables")
def tables(req: TablesRequest):
    try:
        c = mysql_connect(req.connection)
        try:
            return list_tables(c, req.database)
        finally:
            c.close()
    except DB_ERRORS as e:
        raise _fail(e)


@app.post("/api/migrations/preview")
def preview(req: MigrationRequest):
    try:
        plan = make_plan(req)
    except DB_ERRORS as e:
        raise _fail(e)
    return {"sql": plan.to_sql(), "warnings": plan.warnings, "table_count": len(plan.tables)}


@app.post("/api/migrations", status_code=202)
def start_migration(req: MigrationRequest):
    job = jobs.create(req)
    return {"id": job.id}


@app.get("/api/migrations")
def list_migrations():
    return [j.summary() for j in jobs.all()]


@app.get("/api/migrations/{job_id}")
def get_migration(job_id: str, log_after: int = 0):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="Migration not found")
    return job.snapshot(log_after)


@app.post("/api/migrations/{job_id}/cancel")
def cancel_migration(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="Migration not found")
    job.cancel_event.set()
    return {"ok": True}


# ---------------------------------------------------------------------------
# SQL Workbench
# ---------------------------------------------------------------------------

@app.post("/api/workbench/connect")
def workbench_connect(conn: WorkbenchConn):
    try:
        return workbench.test_connection(conn)
    except DB_ERRORS as e:
        raise HTTPException(400, detail=describe_error(e))


@app.post("/api/workbench/schemas")
def workbench_schemas(conn: WorkbenchConn):
    try:
        return workbench.schema_tree(conn)
    except DB_ERRORS as e:
        raise HTTPException(400, detail=describe_error(e))


@app.post("/api/workbench/query")
def workbench_query(q: WorkbenchQuery):
    if not q.sql.strip():
        raise HTTPException(400, detail="Query is empty")
    try:
        return workbench.execute(q)
    except QueryCancelled as e:
        raise HTTPException(409, detail=e.reason)
    except DB_ERRORS as e:
        raise HTTPException(400, detail=describe_error(e))


@app.post("/api/workbench/query/{query_id}/cancel")
def workbench_cancel(query_id: str):
    """Cancel a running query. ``running: false`` means it had not started or already finished."""
    return {"ok": True, "running": running.cancel(query_id)}


# ---------------------------------------------------------------------------
# Schema Dump
# ---------------------------------------------------------------------------

DUMP_DB_ERRORS = (pymysql.Error, psycopg.Error, oracledb.Error, pymssql.Error, OSError)


def _dump_db_error(e: Exception) -> HTTPException:
    if isinstance(e, pymssql.Error) and e.args:
        # pymssql packs (code, b"message ... DB-Lib error ...") into args[0]
        first = e.args[0]
        if isinstance(first, tuple) and len(first) >= 2:
            msg = first[1].decode("utf-8", "replace") if isinstance(first[1], bytes) else str(first[1])
            return HTTPException(400, detail=msg.split("DB-Lib error")[0].strip() or str(e))
    if isinstance(e, oracledb.Error):
        # e.g. "DPY-6005: cannot connect ... DPY-6001: Service "X" is not registered ..." -
        # the useful cause is on the later lines.
        lines = [l.strip() for l in str(e).strip().splitlines() if l.strip() and not l.strip().startswith("Help:")]
        return HTTPException(400, detail=" ".join(lines)[:500])
    return HTTPException(400, detail=describe_error(e))


@app.get("/api/dump/config")
def dump_config():
    return {"root": DUMP_ROOT_DISPLAY}


@app.post("/api/dump/connect")
def dump_connect(conn: DumpConn):
    """Connect, then list schemas with what this user may dump (schema DDL / data)."""
    try:
        return dump_service.inspect(conn)
    except DumpError as e:
        raise HTTPException(400, detail=str(e))
    except DUMP_DB_ERRORS as e:
        raise _dump_db_error(e)


@app.post("/api/dump/jobs", status_code=202)
def dump_start(req: DumpRequest):
    try:
        job = dump_service.start(req)
    except dump_service.DumpExists as e:
        raise HTTPException(409, detail=str(e))
    except DumpError as e:
        raise HTTPException(400, detail=str(e))
    except DUMP_DB_ERRORS as e:
        raise _dump_db_error(e)
    return {"id": job.id}


@app.get("/api/dump/jobs/{job_id}")
def dump_status(job_id: str, log_after: int = 0):
    job = dump_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="Dump job not found")
    return job.snapshot(log_after)


@app.post("/api/dump/jobs/{job_id}/cancel")
def dump_cancel(job_id: str):
    job = dump_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="Dump job not found")
    job.cancel_event.set()
    return {"ok": True}


@app.get("/api/dump/jobs/{job_id}/download")
def dump_download(job_id: str):
    job = dump_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="Dump job not found")
    if job.status != "completed" or not job.target.is_file():
        raise HTTPException(409, detail="The dump file is not available")
    return FileResponse(job.target, media_type="application/sql", filename=job.target.name)
