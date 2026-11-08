"""Connection helpers for the MySQL source and PostgreSQL target."""

from __future__ import annotations

import psycopg
import pymysql
import pymysql.cursors

from .models import MySQLConn, PGConn

SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}


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
