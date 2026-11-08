"""SQL Workbench: connect to any MySQL or PostgreSQL server, browse it, run SQL."""

from __future__ import annotations

from .db import SYSTEM_DATABASES, mysql_connect, pg_connect
from .models import WorkbenchConn, WorkbenchQuery
from .query import run_mysql, run_postgres

MAX_TABLES_PER_SCHEMA = 2000
PG_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")


def test_connection(c: WorkbenchConn) -> dict:
    if c.engine == "mysql":
        conn = mysql_connect(c.as_mysql(), database=c.database.strip() or None)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION() AS v, DATABASE() AS d")
                row = cur.fetchone()
            return {"ok": True, "engine": "mysql", "version": row["v"], "database": row["d"] or ""}
        finally:
            conn.close()
    with pg_connect(c.as_postgres()) as conn:
        version, database = conn.execute("SELECT current_setting('server_version'), current_database()").fetchone()
    return {"ok": True, "engine": "postgres", "version": version, "database": database}


def schema_tree(c: WorkbenchConn) -> list[dict]:
    """Schemas (MySQL databases / PostgreSQL schemas) with their tables and views.

    System schemas are left out. Each schema lists at most MAX_TABLES_PER_SCHEMA objects.
    """
    if c.engine == "mysql":
        conn = mysql_connect(c.as_mysql())
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT SCHEMA_NAME AS s FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")
                names = [r["s"] for r in cur.fetchall() if r["s"] not in SYSTEM_DATABASES]
                cur.execute(
                    "SELECT TABLE_SCHEMA AS s, TABLE_NAME AS t, TABLE_TYPE AS k FROM information_schema.TABLES "
                    "ORDER BY TABLE_SCHEMA, TABLE_NAME"
                )
                objects = [(r["s"], r["t"], "view" if r["k"] == "VIEW" else "table") for r in cur.fetchall()]
        finally:
            conn.close()
    else:
        with pg_connect(c.as_postgres()) as conn:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT nspname FROM pg_namespace WHERE nspname <> ALL(%s) "
                    "AND nspname NOT LIKE 'pg\\_toast%%' AND nspname NOT LIKE 'pg\\_temp\\_%%' ORDER BY nspname",
                    (list(PG_SYSTEM_SCHEMAS),),
                )
            ]
            objects = [
                (s, t, "view" if k == "VIEW" else "table")
                for s, t, k in conn.execute(
                    "SELECT table_schema, table_name, table_type FROM information_schema.tables "
                    "WHERE table_schema <> ALL(%s) ORDER BY table_schema, table_name",
                    (list(PG_SYSTEM_SCHEMAS),),
                )
            ]

    tree = {name: {"name": name, "tables": [], "truncated": False} for name in names}
    for schema, name, kind in objects:
        node = tree.get(schema)
        if node is None:
            continue
        if len(node["tables"]) >= MAX_TABLES_PER_SCHEMA:
            node["truncated"] = True
            continue
        node["tables"].append({"name": name, "type": kind})
    return list(tree.values())


def execute(q: WorkbenchQuery) -> dict:
    schema = q.default_schema.strip()
    if q.connection.engine == "mysql":
        return run_mysql(
            q.connection.as_mysql(),
            q.sql,
            database=schema or q.connection.database.strip() or None,
            read_only=q.read_only,
            max_rows=q.max_rows,
            timeout_ms=q.timeout_ms,
            query_id=q.query_id,
        )
    return run_postgres(
        q.connection.as_postgres(),
        q.sql,
        search_path=[schema] if schema else None,
        read_only=q.read_only,
        max_rows=q.max_rows,
        timeout_ms=q.timeout_ms,
        query_id=q.query_id,
    )
