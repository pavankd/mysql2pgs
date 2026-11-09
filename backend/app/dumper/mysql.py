"""MySQL / MariaDB: permission check from information_schema grants; the dump is
generated with the same SHOW CREATE statements mysqldump uses.

Not shelling out to mysqldump: current MariaDB clients query columns MySQL 5.6 lacks
(information_schema.COLUMNS.generation_expression), and generating the script here keeps
zero dates and other server-specific values byte-exact.
"""

from __future__ import annotations

import re
from collections import defaultdict

import pymysql
import pymysql.cursors
from pymysql.constants import FIELD_TYPE
from pymysql.converters import escape_string

from ..db import SYSTEM_DATABASES, mysql_connect
from ..models import DumpConn, MySQLConn
from ..query import _MYSQL_CONV
from .base import DumpJob, DumpWriter

MAX_INSERT_BYTES = 1_000_000  # well under the default max_allowed_packet (4 MB)
BINARY_CHARSET = 63


def _conn(c: DumpConn) -> MySQLConn:
    return MySQLConn(host=c.host, port=c.port, user=c.user, password=c.password)


def _like(pattern: str, name: str) -> bool:
    """MySQL grant patterns use LIKE wildcards (`_`, `%`) with backslash escapes."""
    rx, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            rx += re.escape(pattern[i + 1])
            i += 2
            continue
        rx += ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
        i += 1
    return re.fullmatch(rx, name, flags=re.S) is not None


def inspect(c: DumpConn) -> dict:
    conn = mysql_connect(_conn(c))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS v, CURRENT_USER() AS u")
            row = cur.fetchone()
            version, current_user = row["v"], row["u"]
            user, _, host = current_user.rpartition("@")
            grantee = f"'{user}'@'{host}'"

            cur.execute("SELECT PRIVILEGE_TYPE AS p FROM information_schema.USER_PRIVILEGES WHERE GRANTEE = %s", (grantee,))
            global_privs = {r["p"] for r in cur.fetchall()}
            cur.execute(
                "SELECT TABLE_SCHEMA AS s, PRIVILEGE_TYPE AS p FROM information_schema.SCHEMA_PRIVILEGES WHERE GRANTEE = %s",
                (grantee,),
            )
            schema_grants = [(r["s"], r["p"]) for r in cur.fetchall()]
            cur.execute(
                "SELECT TABLE_SCHEMA AS s, TABLE_NAME AS t, PRIVILEGE_TYPE AS p "
                "FROM information_schema.TABLE_PRIVILEGES WHERE GRANTEE = %s",
                (grantee,),
            )
            table_grants: dict[tuple[str, str], set[str]] = defaultdict(set)
            for r in cur.fetchall():
                table_grants[(r["s"], r["t"])].add(r["p"])

            cur.execute("SELECT SCHEMA_NAME AS s FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")
            names = [r["s"] for r in cur.fetchall() if r["s"] not in SYSTEM_DATABASES]
            cur.execute("SELECT TABLE_SCHEMA AS s, TABLE_NAME AS t, TABLE_TYPE AS k FROM information_schema.TABLES")
            objects: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for r in cur.fetchall():
                objects[r["s"]].append((r["t"], "view" if r["k"] == "VIEW" else "table"))
            counts = {}
            for kind, sql in (
                ("routines", "SELECT ROUTINE_SCHEMA AS s, COUNT(*) AS n FROM information_schema.ROUTINES GROUP BY ROUTINE_SCHEMA"),
                ("triggers", "SELECT TRIGGER_SCHEMA AS s, COUNT(*) AS n FROM information_schema.TRIGGERS GROUP BY TRIGGER_SCHEMA"),
                ("events", "SELECT EVENT_SCHEMA AS s, COUNT(*) AS n FROM information_schema.EVENTS GROUP BY EVENT_SCHEMA"),
            ):
                cur.execute(sql)
                counts[kind] = {r["s"]: int(r["n"]) for r in cur.fetchall()}
    finally:
        conn.close()

    def has(priv: str, schema: str) -> bool:
        return priv in global_privs or any(p == priv and _like(pat, schema) for pat, p in schema_grants)

    # Routine bodies: 5.6 needs SELECT on mysql.proc; 8.0 SHOW_ROUTINE; global SELECT covers both.
    can_routines = "SELECT" in global_privs or "SHOW_ROUTINE" in global_privs or has("SELECT", "mysql")

    schemas = []
    for name in names:
        objs = objects.get(name, [])
        tables = [t for t, k in objs if k == "table"]
        views = [t for t, k in objs if k == "view"]
        # Tables without any privilege are invisible in information_schema, so with only
        # table-level grants a complete dump cannot be verified: require schema-level SELECT.
        schema_select = has("SELECT", name)
        no_show_view = [v for v in views if not (has("SHOW VIEW", name) or "SHOW VIEW" in table_grants.get((name, v), set()))]
        n_routines = counts["routines"].get(name, 0)
        n_triggers = counts["triggers"].get(name, 0)
        n_events = counts["events"].get(name, 0)

        schema_issues, data_issues, notes = [], [], []
        if not schema_select:
            msg = ("SELECT on the whole schema is missing (table-level grants hide the other tables, "
                   "so a complete dump can't be verified)")
            schema_issues.append(msg)
            data_issues.append(msg)
        if no_show_view:
            schema_issues.append(f"SHOW VIEW privilege missing for {len(no_show_view)} view(s)")
        # MySQL hides triggers/routines/events from users without the matching privilege,
        # so a count of 0 proves nothing: warn on the missing privilege itself.
        if not has("TRIGGER", name):
            notes.append("Triggers can't be read without the TRIGGER privilege; any triggers will be missing")
        if not can_routines:
            notes.append("Routines can't be read without SELECT on mysql.proc (5.6) or SHOW_ROUTINE; any routines will be missing")
        if not has("EVENT", name):
            notes.append("Events can't be read without the EVENT privilege; any events will be missing")
        schemas.append({
            "name": name,
            "tables": len(tables),
            "views": len(views),
            "routines": n_routines,
            "can_dump_schema": not schema_issues,
            "can_dump_data": not data_issues,
            "schema_issues": schema_issues,
            "data_issues": data_issues,
            "notes": notes,
            # Per kind: "ok" or "denied" (privilege to read triggers / routines / events).
            "_flags": {
                "triggers": "ok" if has("TRIGGER", name) else "denied",
                "routines": "ok" if can_routines else "denied",
                "events": "ok" if has("EVENT", name) else "denied",
            },
        })
    return {
        "engine": "mysql",
        "version": version,
        "user": current_user,
        "schemas": schemas,
        "notes": [
            "Permissions are read from information_schema grants; MySQL 8 roles are not expanded.",
        ],
    }




# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------

# Encoders only (for query parameters), no decoders: every value arrives as the server's
# own text (numbers, dates incl. 0000-00-00) or as bytes for binary-charset columns.
_RAW_CONV = {k: v for k, v in _MYSQL_CONV.items() if not isinstance(k, int)}
NUMERIC_TYPES = {
    FIELD_TYPE.DECIMAL, FIELD_TYPE.NEWDECIMAL, FIELD_TYPE.TINY, FIELD_TYPE.SHORT, FIELD_TYPE.LONG,
    FIELD_TYPE.FLOAT, FIELD_TYPE.DOUBLE, FIELD_TYPE.LONGLONG, FIELD_TYPE.INT24, FIELD_TYPE.YEAR,
}
HEADER = """/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
"""
FOOTER = """/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;
/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;
"""
DUMP_SQL_MODE = "NO_AUTO_VALUE_ON_ZERO"


def _mq(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _value(v, type_code: int, charsetnr: int | None) -> str:
    if v is None:
        return "NULL"
    if type_code == FIELD_TYPE.BIT:
        return "b'" + format(int.from_bytes(v, "big"), "b") + "'"
    if isinstance(v, (bytes, bytearray)):
        if charsetnr == BINARY_CHARSET or type_code == FIELD_TYPE.GEOMETRY:
            return "0x" + bytes(v).hex().upper() if v else "''"
        v = bytes(v).decode("utf-8", "replace")
    if type_code in NUMERIC_TYPES:
        return v  # the server's own text: exact for DECIMAL, round-trips FLOAT/DOUBLE
    return "'" + escape_string(v) + "'"


def _open(c: DumpConn):
    conn = pymysql.connect(
        host=c.host, port=c.port, user=c.user, password=c.password, charset="utf8mb4",
        connect_timeout=10, autocommit=True, conv=_RAW_CONV,
    )
    with conn.cursor() as cur:
        cur.execute("SET time_zone = '+00:00'")
        cur.execute("SET SESSION net_write_timeout = 3600, net_read_timeout = 3600")
        # Same consistency as mysqldump --single-transaction (InnoDB snapshot, no locks).
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("START TRANSACTION /*!40100 WITH CONSISTENT SNAPSHOT */")
    return conn


def _rows(conn, sql: str, args=None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def dump(job: DumpJob, writer: DumpWriter, info: dict) -> None:
    req = job.request
    by_name = {s["name"]: s for s in info["schemas"]}
    want_schema = req.content in ("schema", "both")
    want_data = req.content in ("data", "both")
    conn = _open(req.connection)
    try:
        objects: dict[str, list[tuple[str, str]]] = {
            schema: [(t, k) for t, k in _rows(
                conn,
                "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                (schema,),
            )]
            for schema in req.schemas
        }
        tables = {s: [t for t, k in objs if k != "VIEW"] for s, objs in objects.items()}
        views = {s: [t for t, k in objs if k == "VIEW"] for s, objs in objects.items()}
        job.items_total = sum(len(t) for t in tables.values())

        writer.write(HEADER)
        writer.line()

        # 1. Databases and tables (all schemas first, so cross-schema references resolve)
        for schema in req.schemas:
            writer.line(f"-- {'-' * 60}\n-- Database {_mq(schema)}\n-- {'-' * 60}")
            if req.include_create_schema:
                create_db = _rows(conn, f"SHOW CREATE DATABASE {_mq(schema)}")[0][1]
                writer.line(create_db.replace("CREATE DATABASE ", "CREATE DATABASE /*!32312 IF NOT EXISTS*/ ", 1) + ";")
            writer.line(f"USE {_mq(schema)};")
            writer.line()
            if want_schema:
                job.phase = "table definitions"
                for t in tables[schema]:
                    job.check_cancel()
                    job.current = f"{schema}.{t}"
                    writer.line(f"-- Table structure for table {_mq(t)}")
                    writer.line(_rows(conn, f"SHOW CREATE TABLE {_mq(schema)}.{_mq(t)}")[0][1] + ";")
                    writer.line()

        # 2. Data
        if want_data:
            job.phase = "dumping data"
            for schema in req.schemas:
                writer.line(f"USE {_mq(schema)};")
                generated = {
                    (t, col)
                    for t, col in _rows(
                        conn,
                        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = %s AND EXTRA LIKE '%%GENERATED%%'",
                        (schema,),
                    )
                }
                for t in tables[schema]:
                    job.check_cancel()
                    job.current = f"{schema}.{t}"
                    _table_data(conn, writer, job, schema, t, generated)
                    job.items_done += 1
        else:
            job.items_done = job.items_total

        if not want_schema:
            writer.write(FOOTER)
            return

        # 3. Views, ordered so a view comes after the views it reads from
        job.phase = "views and code"
        ordered = _order_views(conn, [(s, v) for s in req.schemas for v in views[s]])
        current_db = None
        for schema, view, ddl in ordered:
            if schema != current_db:
                writer.line(f"USE {_mq(schema)};")
                current_db = schema
            writer.line(f"-- View {_mq(view)}")
            writer.line(ddl + ";")
            writer.line()

        # 4. Routines, triggers (after the data, so they never fire during the load), events
        for schema in req.schemas:
            flags = by_name.get(schema, {}).get("_flags", {})
            writer.line(f"USE {_mq(schema)};")
            if flags.get("routines") == "ok":
                for name, kind in _rows(
                    conn,
                    "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_TYPE, ROUTINE_NAME",
                    (schema,),
                ):
                    row = _rows(conn, f"SHOW CREATE {kind} {_mq(schema)}.{_mq(name)}")[0]
                    _compound(writer, job, f"{kind.lower()} {schema}.{name}", row[1], row[2])
            elif flags.get("routines") == "denied":
                job.log("warning", f"{schema}: routines skipped (no privilege to read their bodies)")
            if flags.get("triggers") == "ok":
                for (name,) in _rows(
                    conn,
                    "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s ORDER BY EVENT_OBJECT_TABLE, ACTION_ORDER",
                    (schema,),
                ):
                    row = _rows(conn, f"SHOW CREATE TRIGGER {_mq(schema)}.{_mq(name)}")[0]
                    _compound(writer, job, f"trigger {schema}.{name}", row[1], row[2])
            elif flags.get("triggers") == "denied":
                job.log("warning", f"{schema}: triggers skipped (TRIGGER privilege missing)")
            if flags.get("events") == "ok":
                for (name,) in _rows(
                    conn, "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME", (schema,)
                ):
                    row = _rows(conn, f"SHOW CREATE EVENT {_mq(schema)}.{_mq(name)}")[0]
                    _compound(writer, job, f"event {schema}.{name}", row[1], row[3])
            elif flags.get("events") == "denied":
                job.log("warning", f"{schema}: events skipped (EVENT privilege missing)")
        writer.line()
        writer.write(FOOTER)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def _compound(writer: DumpWriter, job: DumpJob, label: str, sql_mode: str | None, ddl: str | None) -> None:
    """Routines/triggers/events contain `;`, so they are wrapped in DELIMITER ;; and keep
    the sql_mode they were created with."""
    if ddl is None:
        job.log("warning", f"{label} skipped: its definition is not readable by this user")
        writer.line(f"-- skipped {label}: definition not readable")
        return
    writer.line(f"-- {label}")
    writer.line(f"/*!50003 SET sql_mode = '{escape_string(sql_mode or '')}' */;")
    writer.line("DELIMITER ;;")
    writer.line(ddl + " ;;")
    writer.line("DELIMITER ;")
    writer.line(f"/*!50003 SET sql_mode = '{DUMP_SQL_MODE}' */;")
    writer.line()


def _order_views(conn, views: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    ddl = {(s, v): _rows(conn, f"SHOW CREATE VIEW {_mq(s)}.{_mq(v)}")[0][1] for s, v in views}
    deps = {
        key: {other for other in ddl if other != key and f"{_mq(other[0])}.{_mq(other[1])}" in text}
        for key, text in ddl.items()
    }
    ordered: list[tuple[str, str, str]] = []
    done: set[tuple[str, str]] = set()
    while len(done) < len(ddl):
        ready = [k for k in ddl if k not in done and deps[k] <= done] or [k for k in ddl if k not in done]
        for k in sorted(ready):
            ordered.append((k[0], k[1], ddl[k]))
            done.add(k)
    return ordered


def _table_data(conn, writer: DumpWriter, job: DumpJob, schema: str, table: str, generated: set) -> None:
    cur = conn.cursor(pymysql.cursors.SSCursor)
    try:
        cur.execute(f"SELECT * FROM {_mq(schema)}.{_mq(table)}")
        fields = cur._result.fields  # charsetnr tells binary from text columns
        keep = [i for i, d in enumerate(cur.description) if (table, d[0]) not in generated]
        col_list = ", ".join(_mq(cur.description[i][0]) for i in keep)
        prefix = f"INSERT INTO {_mq(table)} ({col_list}) VALUES "
        writer.line(f"-- Dumping data for table {_mq(table)}")
        writer.line(f"/*!40000 ALTER TABLE {_mq(table)} DISABLE KEYS */;")
        batch: list[str] = []
        size = n = 0
        for row in cur.fetchall_unbuffered():
            tup = "(" + ",".join(_value(row[i], fields[i].type_code, fields[i].charsetnr) for i in keep) + ")"
            if batch and size + len(tup) > MAX_INSERT_BYTES:
                writer.line(prefix + ",".join(batch) + ";")
                batch, size = [], 0
                job.check_cancel()
            batch.append(tup)
            size += len(tup) + 1
            n += 1
            job.rows += 1
        if batch:
            writer.line(prefix + ",".join(batch) + ";")
        writer.line(f"/*!40000 ALTER TABLE {_mq(table)} ENABLE KEYS */;")
        writer.line()
        job.log("info", f"{schema}.{table}: {n:,} row(s)")
    finally:
        cur.close()
