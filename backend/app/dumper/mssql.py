"""SQL Server: permission check with HAS_PERMS_BY_NAME; DDL generated from catalog
views, data as INSERT statements. The output is a sqlcmd / SSMS script (GO batches)."""

from __future__ import annotations

import math
import re

import pymssql

from ..models import DumpConn
from .base import DumpError, DumpJob, DumpWriter

ROWS_PER_INSERT = 100  # SQL Server allows up to 1000 rows per VALUES list
INSERTS_PER_BATCH = 50
TEMPORAL = {"date", "datetime", "datetime2", "smalldatetime", "time", "datetimeoffset"}
BINARY = {"binary", "varbinary", "image"}
STRINGS = {"char", "varchar", "text", "nchar", "nvarchar", "ntext", "sysname"}
NUMBERS = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric", "money", "smallmoney", "float", "real"}
MODULE_TYPES = {"V": "view", "P": "procedure", "FN": "function", "IF": "function", "TF": "function", "TR": "trigger"}


def _connect(c: DumpConn):
    return pymssql.connect(
        server=c.host,
        port=str(c.port),
        user=c.user,
        password=c.password,
        database=c.database.strip() or "master",
        login_timeout=10,
        charset="UTF-8",
        appname="mysql2pgs-dump",
    )


def _q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


_NEWLINE = re.compile(r"(\r\n|\r|\n)")
_NEWLINE_SQL = {"\r\n": "NCHAR(13) + NCHAR(10)", "\r": "NCHAR(13)", "\n": "NCHAR(10)"}


def _n(s: str) -> str:
    """N'...' literal. Line breaks become NCHAR() so no literal spans lines: SSMS and
    sqlcmd end a batch at any line consisting of GO, even inside a string."""
    if "\n" not in s and "\r" not in s:
        return "N'" + s.replace("'", "''") + "'"
    parts = []
    for piece in _NEWLINE.split(s):
        if piece in _NEWLINE_SQL:
            parts.append(_NEWLINE_SQL[piece])
        elif piece:
            parts.append("N'" + piece.replace("'", "''") + "'")
    expr = " + ".join(parts)
    # Concatenating short literals yields nvarchar(4000) and would silently truncate.
    return f"CAST(N'' AS nvarchar(max)) + {expr}" if len(s) > 4000 else expr


def inspect(c: DumpConn) -> dict:
    conn = _connect(c)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)), CAST(SERVERPROPERTY('Edition') AS nvarchar(128)),"
            " SUSER_SNAME(), DB_NAME()"
        )
        version, edition, login, database = cur.fetchone()
        cur.execute(
            """
            SELECT s.name,
                   HAS_PERMS_BY_NAME(QUOTENAME(s.name), 'SCHEMA', 'VIEW DEFINITION'),
                   HAS_PERMS_BY_NAME(QUOTENAME(s.name), 'SCHEMA', 'SELECT'),
                   (SELECT COUNT(*) FROM sys.tables t WHERE t.schema_id = s.schema_id AND t.is_ms_shipped = 0),
                   (SELECT COUNT(*) FROM sys.views v WHERE v.schema_id = s.schema_id AND v.is_ms_shipped = 0),
                   (SELECT COUNT(*) FROM sys.objects o WHERE o.schema_id = s.schema_id AND o.is_ms_shipped = 0
                      AND o.type IN ('P', 'FN', 'IF', 'TF', 'TR')),
                   (SELECT COUNT(*) FROM sys.tables t WHERE t.schema_id = s.schema_id AND t.is_ms_shipped = 0
                      AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(t.name), 'OBJECT', 'SELECT') = 0),
                   (SELECT COUNT(*) FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id
                      WHERE o.schema_id = s.schema_id AND m.definition IS NULL)
            FROM sys.schemas s
            WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') AND s.schema_id < 16384
            ORDER BY s.name
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    schemas = []
    for name, view_def, select, tables, views, routines, no_select, encrypted in rows:
        schema_issues, data_issues, notes = [], [], []
        if not view_def:
            schema_issues.append("VIEW DEFINITION permission on the schema is missing")
        if not select:
            if not view_def:
                data_issues.append("SELECT on the schema is missing, and without VIEW DEFINITION not all tables are visible")
            elif no_select:
                data_issues.append(f"SELECT permission missing on {no_select} of {tables} tables")
        if encrypted:
            notes.append(f"{encrypted} encrypted module(s) can't be scripted and will be skipped")
        schemas.append({
            "name": name,
            "tables": tables,
            "views": views,
            "routines": routines,
            "can_dump_schema": not schema_issues,
            "can_dump_data": not data_issues,
            "schema_issues": schema_issues,
            "data_issues": data_issues,
            "notes": notes,
        })
    return {
        "engine": "mssql",
        "version": f"{version} ({edition})",
        "user": login,
        "database": database,
        "schemas": schemas,
        "notes": [f"Schemas of database \"{database}\". Output is a sqlcmd / SSMS script with GO batches."],
    }


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

def _type_sql(name: str, max_length: int, precision: int, scale: int, user_defined: bool, type_schema: str) -> str:
    if user_defined:
        return f"{_q(type_schema)}.{_q(name)}"
    t = name.lower()
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if max_length == -1 else max_length})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if max_length == -1 else max_length // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({precision}, {scale})"
    if t in ("datetime2", "time", "datetimeoffset"):
        return f"{t}({scale})"
    if t == "float":
        return "float" if precision == 53 else f"float({precision})"
    return t


def _columns(cur, object_id: int) -> list[dict]:
    cur.execute(
        """
        SELECT c.name, ty.name, c.max_length, c.precision, c.scale, ty.is_user_defined, SCHEMA_NAME(ty.schema_id),
               c.is_nullable, c.is_identity, CAST(ic.seed_value AS nvarchar(64)), CAST(ic.increment_value AS nvarchar(64)),
               c.is_computed, cc.definition, cc.is_persisted, c.collation_name, dc.name, dc.definition,
               TYPE_NAME(c.system_type_id), c.is_rowguidcol,
               CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128))
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
        WHERE c.object_id = %s
        ORDER BY c.column_id
        """,
        (object_id,),
    )
    cols = []
    for (name, tname, max_len, prec, scale, udt, tschema, nullable, identity, seed, incr, computed, cdef,
         persisted, collation, dname, ddef, base, rowguid, db_collation) in cur.fetchall():
        cols.append({
            "name": name, "type": _type_sql(tname, max_len, prec, scale, udt, tschema), "base": (base or tname).lower(),
            "nullable": nullable, "identity": identity, "seed": seed, "increment": incr,
            "computed": computed, "computed_def": cdef, "persisted": persisted,
            "collation": collation if collation and collation != db_collation else None,
            "default_name": dname, "default_def": ddef, "rowguid": rowguid,
        })
    return cols


def _index_columns(cur, object_id: int) -> dict[int, dict[str, list[str]]]:
    cur.execute(
        """
        SELECT ic.index_id, c.name, ic.is_descending_key, ic.is_included_column
        FROM sys.index_columns ic
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE ic.object_id = %s
        ORDER BY ic.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id
        """,
        (object_id,),
    )
    out: dict[int, dict[str, list[str]]] = {}
    for index_id, col, desc, included in cur.fetchall():
        entry = out.setdefault(index_id, {"keys": [], "include": []})
        if included:
            entry["include"].append(_q(col))
        else:
            entry["keys"].append(f"{_q(col)} {'DESC' if desc else 'ASC'}")
    return out


def _create_table(cur, schema: str, table: str, object_id: int) -> str:
    cols = _columns(cur, object_id)
    lines = []
    for c in cols:
        if c["computed"]:
            lines.append(f"{_q(c['name'])} AS {c['computed_def']}{' PERSISTED' if c['persisted'] else ''}")
            continue
        parts = [_q(c["name"]), c["type"]]
        if c["collation"]:
            parts.append(f"COLLATE {c['collation']}")
        if c["identity"]:
            parts.append(f"IDENTITY({c['seed']}, {c['increment']})")
        if c["rowguid"]:
            parts.append("ROWGUIDCOL")
        parts.append("NULL" if c["nullable"] else "NOT NULL")
        if c["default_def"] is not None:
            parts.append(f"CONSTRAINT {_q(c['default_name'])} DEFAULT {c['default_def']}")
        lines.append(" ".join(parts))

    idx_cols = _index_columns(cur, object_id)
    cur.execute(
        """
        SELECT kc.name, kc.type, i.type_desc, i.index_id
        FROM sys.key_constraints kc
        JOIN sys.indexes i ON i.object_id = kc.parent_object_id AND i.index_id = kc.unique_index_id
        WHERE kc.parent_object_id = %s
        ORDER BY kc.type, kc.name
        """,
        (object_id,),
    )
    for name, ktype, type_desc, index_id in cur.fetchall():
        kind = "PRIMARY KEY" if ktype.strip() == "PK" else "UNIQUE"
        lines.append(f"CONSTRAINT {_q(name)} {kind} {type_desc} ({', '.join(idx_cols[index_id]['keys'])})")
    cur.execute(
        "SELECT name, definition FROM sys.check_constraints WHERE parent_object_id = %s ORDER BY name", (object_id,)
    )
    for name, definition in cur.fetchall():
        lines.append(f"CONSTRAINT {_q(name)} CHECK {definition}")
    return f"CREATE TABLE {_q(schema)}.{_q(table)} (\n    " + ",\n    ".join(lines) + "\n);"


def _indexes(cur, job: DumpJob, schema: str, table: str, object_id: int) -> list[str]:
    idx_cols = _index_columns(cur, object_id)
    cur.execute(
        """
        SELECT index_id, name, is_unique, type, type_desc, filter_definition
        FROM sys.indexes
        WHERE object_id = %s AND is_primary_key = 0 AND is_unique_constraint = 0 AND is_hypothetical = 0
          AND name IS NOT NULL
        ORDER BY index_id
        """,
        (object_id,),
    )
    out = []
    for index_id, name, unique, itype, type_desc, filt in cur.fetchall():
        if itype not in (1, 2):
            job.log("warning", f"{schema}.{table}: {type_desc.lower()} index {name} not scripted")
            continue
        cols = idx_cols.get(index_id, {"keys": [], "include": []})
        sql = (
            f"CREATE {'UNIQUE ' if unique else ''}{type_desc} INDEX {_q(name)} ON {_q(schema)}.{_q(table)} "
            f"({', '.join(cols['keys'])})"
        )
        if cols["include"]:
            sql += f" INCLUDE ({', '.join(cols['include'])})"
        if filt:
            sql += f" WHERE {filt}"
        out.append(sql + ";")
    return out


def _foreign_keys(cur, schema: str, table: str, object_id: int) -> list[str]:
    cur.execute(
        """
        SELECT fk.object_id, fk.name, OBJECT_SCHEMA_NAME(fk.referenced_object_id), OBJECT_NAME(fk.referenced_object_id),
               fk.delete_referential_action_desc, fk.update_referential_action_desc
        FROM sys.foreign_keys fk WHERE fk.parent_object_id = %s ORDER BY fk.name
        """,
        (object_id,),
    )
    fks = cur.fetchall()
    cur.execute(
        """
        SELECT fkc.constraint_object_id, pc.name, rc.name
        FROM sys.foreign_key_columns fkc
        JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
        JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        WHERE fkc.parent_object_id = %s ORDER BY fkc.constraint_object_id, fkc.constraint_column_id
        """,
        (object_id,),
    )
    cols: dict[int, list[tuple[str, str]]] = {}
    for fk_id, pcol, rcol in cur.fetchall():
        cols.setdefault(fk_id, []).append((pcol, rcol))
    out = []
    for fk_id, name, rschema, rtable, on_delete, on_update in fks:
        pairs = cols.get(fk_id, [])
        out.append(
            f"ALTER TABLE {_q(schema)}.{_q(table)} ADD CONSTRAINT {_q(name)} FOREIGN KEY "
            f"({', '.join(_q(p) for p, _ in pairs)}) REFERENCES {_q(rschema)}.{_q(rtable)} "
            f"({', '.join(_q(r) for _, r in pairs)}) "
            f"ON DELETE {on_delete.replace('_', ' ')} ON UPDATE {on_update.replace('_', ' ')};"
        )
    return out


def _sequences(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT name, TYPE_NAME(user_type_id), CAST(start_value AS nvarchar(64)), CAST(increment AS nvarchar(64)),
               CAST(minimum_value AS nvarchar(64)), CAST(maximum_value AS nvarchar(64)), is_cycling, is_cached,
               cache_size, CAST(last_used_value AS nvarchar(64))
        FROM sys.sequences WHERE schema_id = SCHEMA_ID(%s) ORDER BY name
        """,
        (schema,),
    )
    out = []
    for name, tname, start, incr, minv, maxv, cycling, cached, cache_size, last_used in cur.fetchall():
        restart = start
        if last_used is not None:
            restart = str(int(last_used) + int(incr))  # continue after the last value handed out
        cache = "NO CACHE" if not cached else f"CACHE {cache_size}" if cache_size else "CACHE"
        out.append(
            f"CREATE SEQUENCE {_q(schema)}.{_q(name)} AS {tname} START WITH {restart} INCREMENT BY {incr} "
            f"MINVALUE {minv} MAXVALUE {maxv} {'CYCLE' if cycling else 'NO CYCLE'} {cache};"
        )
    return out


def _alias_types(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT name, TYPE_NAME(system_type_id), max_length, precision, scale, is_nullable
        FROM sys.types
        WHERE is_user_defined = 1 AND is_table_type = 0 AND is_assembly_type = 0 AND schema_id = SCHEMA_ID(%s)
        ORDER BY name
        """,
        (schema,),
    )
    return [
        f"CREATE TYPE {_q(schema)}.{_q(name)} FROM {_type_sql(base, ml, p, s, False, '')} {'NULL' if nullable else 'NOT NULL'};"
        for name, base, ml, p, s, nullable in cur.fetchall()
    ]


def _modules(cur, schema: str) -> list[tuple[str, str, str | None, bool, bool]]:
    cur.execute(
        """
        SELECT o.name, RTRIM(o.type), m.definition, m.uses_ansi_nulls, m.uses_quoted_identifier
        FROM sys.objects o JOIN sys.sql_modules m ON m.object_id = o.object_id
        WHERE o.schema_id = SCHEMA_ID(%s) AND o.is_ms_shipped = 0 AND o.type IN ('V', 'P', 'FN', 'IF', 'TF', 'TR')
        ORDER BY o.object_id
        """,
        (schema,),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _select_expr(c: dict) -> str:
    q, base = _q(c["name"]), c["base"]
    if base in TEMPORAL:
        return f"CONVERT(nvarchar(40), {q}, 126)"
    if base == "uniqueidentifier":
        return f"CONVERT(nvarchar(36), {q})"
    if base in ("xml", "sql_variant"):
        return f"CONVERT(nvarchar(max), {q})"
    if base == "hierarchyid":
        return f"{q}.ToString()"
    if base in ("geometry", "geography"):
        return f"CASE WHEN {q} IS NULL THEN NULL ELSE CONCAT({q}.STSrid, N'|', {q}.AsTextZM()) END"
    return q


def _literal(c: dict, v) -> str:
    if v is None:
        return "NULL"
    base = c["base"]
    if base == "bit":
        return "1" if v else "0"
    if base in NUMBERS:
        if isinstance(v, float) and not math.isfinite(v):
            raise DumpError(f"Non-finite float in column {c['name']}")
        return repr(v) if isinstance(v, float) else str(v)
    if base in BINARY:
        return "0x" + bytes(v).hex().upper()
    if base == "hierarchyid":
        return f"CAST({_n(v)} AS hierarchyid)"
    if base in ("geometry", "geography"):
        srid, _, wkt = str(v).partition("|")
        return f"{base}::STGeomFromText({_n(wkt)}, {int(srid)})"
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    return _n(str(v))


def _table_data(conn, writer: DumpWriter, job: DumpJob, schema: str, table: str, object_id: int) -> None:
    cur = conn.cursor()
    cols = [c for c in _columns(cur, object_id) if not c["computed"] and c["base"] not in ("timestamp", "rowversion")]
    if not cols:
        return
    if any(c["base"] == "sql_variant" for c in cols):
        job.log("warning", f"{schema}.{table}: sql_variant values are dumped as nvarchar")
    qualified = f"{_q(schema)}.{_q(table)}"
    identity = any(c["identity"] for c in cols)
    col_list = ", ".join(_q(c["name"]) for c in cols)
    writer.line(f"-- Data for {qualified}")
    if identity:
        writer.line(f"SET IDENTITY_INSERT {qualified} ON;")
    cur.execute(f"SELECT {', '.join(_select_expr(c) for c in cols)} FROM {qualified}")
    n = statements = 0
    batch: list[str] = []

    def flush():
        nonlocal statements
        if not batch:
            return
        writer.line(f"INSERT INTO {qualified} ({col_list}) VALUES\n  " + ",\n  ".join(batch) + ";")
        batch.clear()
        statements += 1
        if statements % INSERTS_PER_BATCH == 0:
            writer.line("GO")

    while True:
        rows = cur.fetchmany(500)
        if not rows:
            break
        for row in rows:
            batch.append("(" + ", ".join(_literal(c, v) for c, v in zip(cols, row)) + ")")
            n += 1
            job.rows += 1
            if len(batch) >= ROWS_PER_INSERT:
                flush()
        job.check_cancel()
    flush()
    if identity:
        writer.line(f"SET IDENTITY_INSERT {qualified} OFF;")
    writer.line("GO")
    writer.line()
    job.log("info", f"{schema}.{table}: {n:,} row(s)")


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------

def dump(job: DumpJob, writer: DumpWriter, info: dict) -> None:
    req = job.request
    want_schema = req.content in ("schema", "both")
    want_data = req.content in ("data", "both")
    conn = _connect(req.connection)
    try:
        cur = conn.cursor()
        tables: dict[str, list[tuple[str, int]]] = {}
        for schema in req.schemas:
            cur.execute(
                "SELECT name, object_id FROM sys.tables WHERE schema_id = SCHEMA_ID(%s) AND is_ms_shipped = 0 ORDER BY name",
                (schema,),
            )
            tables[schema] = cur.fetchall()
        job.items_total = sum(len(t) for t in tables.values())

        writer.line(f"-- Run with sqlcmd -i <file> (or open in SSMS / Azure Data Studio). Database: {info.get('database', '')}")
        writer.line("SET NOCOUNT ON;")
        writer.line("SET ANSI_NULLS ON;")
        writer.line("SET QUOTED_IDENTIFIER ON;")
        writer.line("GO")
        writer.line()

        # Phases run across all selected schemas, so cross-schema references restore cleanly.
        if want_schema or req.include_create_schema:
            for schema in req.schemas:
                if req.include_create_schema:
                    writer.line(f"IF SCHEMA_ID({_n(schema)}) IS NULL EXEC(N'CREATE SCHEMA {_q(schema).replace(chr(39), chr(39) * 2)}');")
                    writer.line("GO")
            writer.line()

        if want_schema:
            job.phase = "creating tables"
            for schema in req.schemas:
                for stmt in _alias_types(cur, schema) + _sequences(cur, schema):
                    writer.line(stmt)
                    writer.line("GO")
                for table, object_id in tables[schema]:
                    job.check_cancel()
                    job.current = f"{schema}.{table}"
                    writer.line(_create_table(cur, schema, table, object_id))
                    writer.line("GO")
                    writer.line()

        if want_data:
            job.phase = "dumping data"
            if req.content == "data":
                for schema in req.schemas:
                    for table, _ in tables[schema]:
                        writer.line(f"ALTER TABLE {_q(schema)}.{_q(table)} NOCHECK CONSTRAINT ALL;")
                        writer.line(f"DISABLE TRIGGER ALL ON {_q(schema)}.{_q(table)};")
                writer.line("GO")
                writer.line()
            for schema in req.schemas:
                for table, object_id in tables[schema]:
                    job.check_cancel()
                    job.current = f"{schema}.{table}"
                    _table_data(conn, writer, job, schema, table, object_id)
                    job.items_done += 1
            if req.content == "data":
                for schema in req.schemas:
                    for table, _ in tables[schema]:
                        writer.line(f"ALTER TABLE {_q(schema)}.{_q(table)} WITH CHECK CHECK CONSTRAINT ALL;")
                        writer.line(f"ENABLE TRIGGER ALL ON {_q(schema)}.{_q(table)};")
                writer.line("GO")
                writer.line()
        elif want_schema:
            job.items_done = job.items_total

        if want_schema:
            job.phase = "indexes, keys and code"
            for schema in req.schemas:
                for table, object_id in tables[schema]:
                    for stmt in _indexes(cur, job, schema, table, object_id):
                        writer.line(stmt)
                if tables[schema]:
                    writer.line("GO")
            for schema in req.schemas:
                for table, object_id in tables[schema]:
                    for stmt in _foreign_keys(cur, schema, table, object_id):
                        writer.line(stmt)
                if tables[schema]:
                    writer.line("GO")
            writer.line()
            modules = [(s, m) for s in req.schemas for m in _modules(cur, s)]
            # Triggers last, so they never fire while the data above is loaded.
            for schema, (name, otype, definition, ansi_nulls, quoted_ident) in sorted(
                modules, key=lambda sm: sm[1][1] == "TR"
            ):
                if definition is None:
                    job.log("warning", f"{schema}.{name}: encrypted {MODULE_TYPES.get(otype, otype)} skipped")
                    writer.line(f"-- skipped encrypted {MODULE_TYPES.get(otype, otype)} {_q(schema)}.{_q(name)}")
                    continue
                writer.line(f"SET ANSI_NULLS {'ON' if ansi_nulls else 'OFF'};")
                writer.line(f"SET QUOTED_IDENTIFIER {'ON' if quoted_ident else 'OFF'};")
                writer.line("GO")
                writer.line(definition.strip())
                writer.line("GO")
                writer.line()
    finally:
        conn.close()
