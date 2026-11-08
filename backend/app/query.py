"""Ad-hoc SQL execution for the migration console and the SQL Workbench.

Both engines return the same shape: one result set per statement, values made
JSON-safe without losing precision, rows capped at ``max_rows``.

Cancellation: a running query registers a cancel callback under its client-chosen
``query_id``. PostgreSQL uses the protocol cancel request (``cancel_safe``); MySQL 5.6
has no equivalent, so a second connection sends ``KILL QUERY <thread id>``. MySQL 5.6
also lacks ``max_execution_time``, so its timeout is a timer that triggers the same kill.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import threading
import time
import uuid
from collections.abc import Callable

import psycopg
import pymysql
import pymysql.cursors
from psycopg import sql
from psycopg.types.string import TextLoader
from pymysql.constants import CLIENT, FIELD_TYPE
from pymysql.converters import conversions as MYSQL_CONVERSIONS

from .db import pg_connect
from .models import MySQLConn, PGConn, QueryRequest

MAX_RESULT_SETS = 20
JS_SAFE_INT = 2**53 - 1
# Read temporal values as the server's own text output. Python's datetime cannot hold
# PostgreSQL 'infinity'/'-infinity' (which zero dates migrate to), years outside
# 1..9999, MySQL zero dates, or MySQL TIME values beyond 24h.
PG_TEXT_TYPES = ("date", "time", "timetz", "timestamp", "timestamptz", "interval")
MYSQL_TEXT_TYPES = (FIELD_TYPE.DATE, FIELD_TYPE.NEWDATE, FIELD_TYPE.DATETIME, FIELD_TYPE.TIMESTAMP, FIELD_TYPE.TIME)
# Wire-protocol type codes -> the SQL type names MySQL users know.
MYSQL_TYPE_NAMES = {v: k.lower() for k, v in vars(FIELD_TYPE).items() if k.isupper()} | {
    FIELD_TYPE.TINY: "tinyint", FIELD_TYPE.SHORT: "smallint", FIELD_TYPE.INT24: "mediumint",
    FIELD_TYPE.LONG: "int", FIELD_TYPE.LONGLONG: "bigint", FIELD_TYPE.NEWDECIMAL: "decimal",
    FIELD_TYPE.VAR_STRING: "varchar", FIELD_TYPE.STRING: "char", FIELD_TYPE.NEWDATE: "date",
    FIELD_TYPE.TINY_BLOB: "tinyblob", FIELD_TYPE.MEDIUM_BLOB: "mediumblob", FIELD_TYPE.LONG_BLOB: "longblob",
}
MYSQL_QUERY_INTERRUPTED = 1317
MYSQL_BINARY_CHARSET = 63
# MySQL sends the same type code for TEXT and BLOB (and for CHAR/BINARY, VARCHAR/VARBINARY);
# only the column charset tells them apart.
MYSQL_TEXTUAL_NAMES = {
    FIELD_TYPE.TINY_BLOB: ("tinytext", "tinyblob"), FIELD_TYPE.BLOB: ("text", "blob"),
    FIELD_TYPE.MEDIUM_BLOB: ("mediumtext", "mediumblob"), FIELD_TYPE.LONG_BLOB: ("longtext", "longblob"),
    FIELD_TYPE.VAR_STRING: ("varchar", "varbinary"), FIELD_TYPE.STRING: ("char", "binary"),
}


def mysql_type_name(type_code: int, charsetnr: int | None) -> str:
    pair = MYSQL_TEXTUAL_NAMES.get(type_code)
    if pair and charsetnr is not None:
        return pair[1] if charsetnr == MYSQL_BINARY_CHARSET else pair[0]
    return MYSQL_TYPE_NAMES.get(type_code, str(type_code))


class QueryCancelled(Exception):
    """Raised when a query was stopped by the user or by the timeout."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Cancellation registry
# ---------------------------------------------------------------------------

class _Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancel_fns: dict[str, Callable[[], None]] = {}
        # Cancels that arrived before (or after) the query registered; value = reason.
        self._requested: dict[str, str] = {}

    def register(self, qid: str, fn: Callable[[], None]) -> None:
        with self._lock:
            self._cancel_fns[qid] = fn
            pending = qid in self._requested
        if pending:
            fn()

    def unregister(self, qid: str) -> str | None:
        """Forget the query; returns the cancel reason if it was cancelled."""
        with self._lock:
            self._cancel_fns.pop(qid, None)
            return self._requested.pop(qid, None)

    def peek(self, qid: str) -> str | None:
        """Cancel reason for a still-registered query, without consuming it."""
        with self._lock:
            return self._requested.get(qid)

    def cancel(self, qid: str, reason: str = "Query cancelled by user") -> bool:
        with self._lock:
            if len(self._requested) > 1000:  # cancels for queries that never ran
                self._requested.clear()
            self._requested.setdefault(qid, reason)
            fn = self._cancel_fns.get(qid)
        if fn is None:
            return False
        try:
            fn()
        except Exception:  # noqa: BLE001 - best effort; the query may have just finished
            pass
        return True


running = _Registry()


# ---------------------------------------------------------------------------
# Value encoding
# ---------------------------------------------------------------------------

def _jsonable(v):
    """Make a driver value safe for JSON without losing precision."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v if abs(v) <= JS_SAFE_INT else str(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(v).hex()
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if isinstance(v, set):
        return ",".join(sorted(map(str, v)))
    if isinstance(v, (dt.timedelta, uuid.UUID)):
        return str(v)
    return str(v)


def _result(status: str, kind: str, row_count: int, columns=None, rows=None, truncated=False) -> dict:
    return {
        "status": status,
        "kind": kind,  # "rows" (result set) | "command" (DML/DDL/other)
        "columns": columns or [],
        "rows": rows or [],
        "row_count": row_count,
        "truncated": truncated,
    }


def describe_error(e: Exception) -> str:
    """One-line message, with PostgreSQL's position/hint or MySQL's error code."""
    if isinstance(e, pymysql.err.MySQLError) and len(e.args) >= 2:
        return f"Error {e.args[0]}: {e.args[1]}"
    diag = getattr(e, "diag", None)
    msg = diag.message_primary if diag and diag.message_primary else (str(e).strip().splitlines() or [repr(e)])[0]
    extra = []
    if diag and diag.statement_position:
        extra.append(f"at character {diag.statement_position}")
    if diag and diag.message_hint:
        extra.append(f"hint: {diag.message_hint}")
    return msg + (f" ({'; '.join(extra)})" if extra else "")


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

def run_postgres(
    c: PGConn,
    statement: str,
    *,
    search_path: list[str] | None = None,
    read_only: bool = True,
    max_rows: int = 500,
    timeout_ms: int = 30_000,
    query_id: str | None = None,
) -> dict:
    """Run one or more statements in a single transaction.

    Read-only sets the *session* default as well as the transaction, then rolls back,
    so a ``COMMIT`` inside the script cannot re-enable writes. This guards against
    accidents; it is not a security boundary (the caller holds the credentials).
    Otherwise the whole script commits at the end, or nothing does if a statement fails.
    """
    started = time.monotonic()
    results: list[dict] = []
    extra_sets = 0
    notices: list[str] = []
    with pg_connect(c) as conn:
        if query_id:
            running.register(query_id, conn.cancel_safe)
        try:
            for name in PG_TEXT_TYPES:
                conn.adapters.register_loader(name, TextLoader)
            conn.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(timeout_ms)))
            if search_path:
                conn.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.SQL(", ").join([sql.Identifier(s) for s in search_path] + [sql.Identifier("public")])
                    )
                )
            if read_only:
                conn.execute("SET default_transaction_read_only = on")
            conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))

            with conn.transaction() as tx:
                if read_only:
                    conn.execute("SET TRANSACTION READ ONLY")
                cur = conn.cursor()
                cur.execute(statement)
                while True:
                    if len(results) >= MAX_RESULT_SETS:
                        extra_sets += 1
                    elif cur.description:
                        rows = cur.fetchmany(max_rows + 1)
                        results.append(_result(
                            cur.statusmessage or "",
                            "rows",
                            cur.rowcount,
                            [{"name": d.name, "type": _pg_type_name(conn, d.type_code)} for d in cur.description],
                            [[_jsonable(v) for v in r] for r in rows[:max_rows]],
                            len(rows) > max_rows,
                        ))
                    else:
                        results.append(_result(cur.statusmessage or "", "command", cur.rowcount))
                    if not cur.nextset():
                        break
                current_schema = conn.execute("SELECT current_schema()").fetchone()[0]
                if read_only:
                    raise psycopg.Rollback(tx)  # leave the database exactly as it was
        except psycopg.errors.QueryCanceled as e:
            reason = running.unregister(query_id) if query_id else None
            raise QueryCancelled(reason or "Query cancelled: statement timeout exceeded") from e
        finally:
            if query_id:
                running.unregister(query_id)

    return {
        "results": results,
        "omitted_result_sets": extra_sets,
        "notices": notices,
        "read_only": read_only,
        "current_schema": current_schema,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }


def _pg_type_name(conn: psycopg.Connection, oid: int) -> str:
    info = conn.adapters.types.get(oid)
    return info.name if info else str(oid)


def run_query(req: QueryRequest) -> dict:
    """Migration console entry point (PostgreSQL target, search_path = migrated schemas)."""
    return run_postgres(
        req.connection,
        req.sql,
        search_path=req.search_path,
        read_only=req.read_only,
        max_rows=req.max_rows,
        timeout_ms=req.timeout_ms,
    )


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------

def _as_text(v):
    return v.decode("utf-8", errors="replace") if isinstance(v, (bytes, bytearray)) else v


def _mysql_columns(cur) -> list[dict]:
    # Field metadata (with charset) lives on the result object; fall back to type codes only.
    fields = getattr(getattr(cur, "_result", None), "fields", None) or []
    charsets = [getattr(f, "charsetnr", None) for f in fields] if len(fields) == len(cur.description) else []
    return [
        {"name": d[0], "type": mysql_type_name(d[1], charsets[i] if charsets else None)}
        for i, d in enumerate(cur.description)
    ]


_MYSQL_CONV = dict(MYSQL_CONVERSIONS)
for _t in MYSQL_TEXT_TYPES:
    _MYSQL_CONV[_t] = _as_text


def _mysql_kwargs(c: MySQLConn) -> dict:
    return dict(host=c.host, port=c.port, user=c.user, password=c.password, charset="utf8mb4", connect_timeout=10)


def run_mysql(
    c: MySQLConn,
    statement: str,
    *,
    database: str | None = None,
    read_only: bool = True,
    max_rows: int = 500,
    timeout_ms: int = 30_000,
    query_id: str | None = None,
) -> dict:
    """Run one or more statements (autocommit: each write commits as it runs).

    Rows are streamed with an unbuffered cursor, so a huge result is counted rather
    than held in memory. Read-only uses ``SET SESSION TRANSACTION READ ONLY`` (5.6.5+),
    which blocks DML and DDL and survives an embedded ``COMMIT``.
    """
    started = time.monotonic()
    results: list[dict] = []
    extra_sets = 0
    conn = pymysql.connect(
        **_mysql_kwargs(c),
        database=database or None,
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
        conv=_MYSQL_CONV,
        cursorclass=pymysql.cursors.SSCursor,
    )
    thread_id = conn.thread_id()

    def kill() -> None:
        k = pymysql.connect(**_mysql_kwargs(c))
        try:
            with k.cursor() as cur:
                cur.execute(f"KILL QUERY {int(thread_id)}")
        finally:
            k.close()

    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        try:
            kill()
        except Exception:  # noqa: BLE001 - the query may have just finished
            pass

    def cancel_reason() -> str | None:
        requested = running.peek(query_id) if query_id else None
        return requested or ("Query cancelled: timeout exceeded" if timed_out.is_set() else None)

    timer = threading.Timer(timeout_ms / 1000, on_timeout)
    timer.daemon = True
    if query_id:
        running.register(query_id, kill)
    timer.start()
    try:
        cur = conn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY" if read_only else "SET SESSION TRANSACTION READ WRITE")
        cur.execute(statement)
        while True:
            if cur.description:
                rows = []
                total = 0
                for row in cur.fetchall_unbuffered():
                    total += 1
                    if total <= max_rows:
                        rows.append([_jsonable(v) for v in row])
                if len(results) < MAX_RESULT_SETS:
                    results.append(_result(
                        f"{total} row(s) returned",
                        "rows",
                        total,
                        _mysql_columns(cur),
                        rows,
                        total > max_rows,
                    ))
                else:
                    extra_sets += 1
            elif len(results) < MAX_RESULT_SETS:
                affected = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
                results.append(_result(f"{affected} row(s) affected", "command", affected))
            else:
                extra_sets += 1
            if not cur.nextset():
                break
        cur.execute("SELECT DATABASE()")
        current_schema = next(iter(cur.fetchall_unbuffered()))[0]
    except pymysql.err.MySQLError as e:
        reason = cancel_reason()
        if reason or (e.args and e.args[0] == MYSQL_QUERY_INTERRUPTED):
            raise QueryCancelled(reason or "Query was interrupted") from e
        raise
    finally:
        timer.cancel()
        reason = cancel_reason()
        if query_id:
            running.unregister(query_id)
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # SLEEP() and some statements return normally when killed; honour the cancel anyway.
    if reason:
        raise QueryCancelled(reason)

    return {
        "results": results,
        "omitted_result_sets": extra_sets,
        "notices": [],
        "read_only": read_only,
        "current_schema": current_schema,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }

