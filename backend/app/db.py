"""Connection helpers for the MySQL source and PostgreSQL target."""

from __future__ import annotations

import psycopg
import pymysql
from psycopg import sql
import pymysql.cursors

from .models import MySQLConn, PGConn

SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
# Databases that exist on every PostgreSQL server; used to reach the server when the
# target database itself does not exist yet.
MAINTENANCE_DATABASES = ("postgres", "template1")


def mysql_connect(c: MySQLConn, database: str | None = None, streaming: bool = False):
    """Open a MySQL connection.

    ``streaming=True`` uses an unbuffered server-side cursor so large tables are
    not loaded into memory. The session time zone is pinned to UTC so TIMESTAMP
    values come back as UTC and can be written to ``timestamptz`` unchanged.
    """
    conn = pymysql.connect(
        host=c.host,
        port=c.port,
        user=c.user,
        password=c.password,
        database=database,
        charset="utf8mb4",
        connect_timeout=10,
        autocommit=True,
        cursorclass=pymysql.cursors.SSCursor if streaming else pymysql.cursors.DictCursor,
    )
    with conn.cursor() as cur:
        cur.execute("SET time_zone = '+00:00'")
        cur.execute("SET SESSION net_write_timeout = 3600, net_read_timeout = 3600")
    return conn


def pg_connect(c: PGConn, autocommit: bool = True) -> psycopg.Connection:
    conn = psycopg.connect(
        host=c.host,
        port=c.port,
        user=c.user,
        password=c.password,
        dbname=c.database,
        connect_timeout=10,
        autocommit=autocommit,
    )
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def mysql_version(c: MySQLConn) -> str:
    conn = mysql_connect(c)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS v")
            return cur.fetchone()["v"]
    finally:
        conn.close()


def pg_version(c: PGConn) -> str:
    with pg_connect(c) as conn:
        return conn.execute("SHOW server_version").fetchone()[0]


def _pg_maintenance_connect(c: PGConn) -> psycopg.Connection:
    last: Exception | None = None
    for name in MAINTENANCE_DATABASES:
        try:
            return pg_connect(c.model_copy(update={"database": name}))
        except psycopg.OperationalError as e:
            last = e
    assert last is not None
    raise last


def pg_probe(c: PGConn) -> dict:
    """Test the target connection, distinguishing "database missing" from other failures.

    libpq reports a missing database only as message text (no SQLSTATE), so instead of
    parsing it we reconnect to a maintenance database and look the name up in pg_database.
    Any other failure (bad host, bad password, no CONNECT privilege) re-raises the original error.
    """
    try:
        return {"ok": True, "version": pg_version(c), "database_missing": False, "note": None}
    except psycopg.OperationalError as original:
        try:
            with _pg_maintenance_connect(c) as m:
                exists = m.execute("SELECT 1 FROM pg_database WHERE datname = %s", (c.database,)).fetchone()
                version = m.execute("SHOW server_version").fetchone()[0]
        except psycopg.Error:
            raise original from None
        if exists:
            raise original
        return {
            "ok": False,
            "version": version,
            "database_missing": True,
            "note": f'Database "{c.database}" does not exist on this server.',
        }


def pg_create_database(c: PGConn) -> dict:
    """CREATE DATABASE <c.database>, owned by the connecting user, with server defaults."""
    with _pg_maintenance_connect(c) as m:
        if m.execute("SELECT 1 FROM pg_database WHERE datname = %s", (c.database,)).fetchone():
            created = False
        else:
            m.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(c.database)))
            created = True
        encoding = m.execute(
            "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = %s", (c.database,)
        ).fetchone()[0]
    note = None if encoding == "UTF8" else f"Database encoding is {encoding}; UTF8 is recommended for MySQL utf8mb4 data."
    return {"ok": True, "created": created, "database": c.database, "encoding": encoding, "note": note}
