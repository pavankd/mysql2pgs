"""Oracle: permission check from session roles/privileges; DDL via DBMS_METADATA,
data as INSERT statements. The output is a SQL*Plus / SQLcl script."""

from __future__ import annotations

import math

import oracledb

from ..models import DumpConn
from .base import DumpError, DumpJob, DumpWriter

# Return CLOB/BLOB as str/bytes and NUMBER as Decimal (no float rounding).
oracledb.defaults.fetch_lobs = False
oracledb.defaults.fetch_decimals = True

CATALOG_ROLES = {"SELECT_CATALOG_ROLE", "EXP_FULL_DATABASE", "DATAPUMP_EXP_FULL_DATABASE", "DBA"}
READ_ANY = {"SELECT ANY TABLE", "READ ANY TABLE"}
NO_DDL = 31608  # ORA-31608: specified object not found (no dependent DDL of that kind)

CHUNK_CHARS = 500  # literal pieces stay far below SQL*Plus' 2499-char line limit
INLINE_LOB_CHUNKS = 20  # bigger LOBs are written through a PL/SQL block
BLOB_CHUNK = 1000  # bytes per HEXTORAW piece
ROWS_PER_COMMIT = 1000

# GET_DDL object type for each ALL_OBJECTS.OBJECT_TYPE we dump (in dependency order).
PLSQL_TYPES = [
    ("FUNCTION", "FUNCTION"),
    ("PROCEDURE", "PROCEDURE"),
    ("PACKAGE", "PACKAGE_SPEC"),
    ("PACKAGE BODY", "PACKAGE_BODY"),
    ("TYPE BODY", "TYPE_BODY"),
    ("SYNONYM", "SYNONYM"),
]


def _connect(c: DumpConn) -> oracledb.Connection:
    service = c.database.strip()
    if not service:
        raise DumpError("Enter the Oracle service name (e.g. FREEPDB1 or ORCLPDB1)")
    return oracledb.connect(
        user=c.user, password=c.password, host=c.host, port=c.port, service_name=service, tcp_connect_timeout=10
    )


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _session(conn) -> tuple[str, set[str], set[str], str]:
    cur = conn.cursor()
    user = cur.execute("SELECT USER FROM dual").fetchone()[0]
    roles = {r for (r,) in cur.execute("SELECT role FROM session_roles")}
    privs = {p for (p,) in cur.execute("SELECT privilege FROM session_privs")}
    # With a catalog role the DBA_* views show every object; otherwise ALL_* shows
    # only what this user can access.
    prefix = "DBA" if roles & CATALOG_ROLES else "ALL"
    return user, roles, privs, prefix


def inspect(c: DumpConn) -> dict:
    with _connect(c) as conn:
        user, roles, privs, prefix = _session(conn)
        cur = conn.cursor()
        names = [r for (r,) in cur.execute(
            "SELECT username FROM all_users WHERE oracle_maintained = 'N' ORDER BY username"
        )]
        counts: dict[str, dict[str, int]] = {}
        for owner, otype, n in cur.execute(
            f"""SELECT o.owner, o.object_type, COUNT(*) FROM {prefix}_objects o
                JOIN all_users u ON u.username = o.owner AND u.oracle_maintained = 'N'
                WHERE o.object_name NOT LIKE 'BIN$%' GROUP BY o.owner, o.object_type"""
        ):
            counts.setdefault(owner, {})[otype] = n
        readable: dict[str, tuple[int, int]] = {}
        for owner, total, ok in cur.execute(
            f"""SELECT t.owner, COUNT(*),
                       SUM(CASE WHEN t.owner = USER OR EXISTS (
                             SELECT 1 FROM all_tab_privs p
                             WHERE p.table_schema = t.owner AND p.table_name = t.table_name
                               AND p.privilege IN ('SELECT', 'READ')) THEN 1 ELSE 0 END)
                FROM {prefix}_tables t
                JOIN all_users u ON u.username = t.owner AND u.oracle_maintained = 'N'
                WHERE t.nested = 'NO' AND t.secondary = 'N' AND t.table_name NOT LIKE 'BIN$%'
                GROUP BY t.owner"""
        ):
            readable[owner] = (total, ok or 0)
        version = conn.version

    catalog = bool(roles & CATALOG_ROLES)
    read_any = bool(privs & READ_ANY)
    schemas = []
    for name in names:
        own = name == user
        cnt = counts.get(name, {})
        total, ok = readable.get(name, (0, 0))
        schema_issues, data_issues, notes = [], [], []
        if not own and not catalog:
            schema_issues.append("SELECT_CATALOG_ROLE is required to extract another schema's DDL")
        if not own and not read_any:
            if not catalog:
                data_issues.append("Without SELECT_CATALOG_ROLE the full table list can't be verified")
            elif ok < total:
                data_issues.append(f"SELECT privilege missing on {total - ok} of {total} tables")
        if not own and not catalog and not cnt:
            notes.append("No objects visible to this user")
        schemas.append({
            "name": name,
            "tables": cnt.get("TABLE", 0),
            "views": cnt.get("VIEW", 0) + cnt.get("MATERIALIZED VIEW", 0),
            "routines": sum(cnt.get(k, 0) for k in ("FUNCTION", "PROCEDURE", "PACKAGE", "TRIGGER", "TYPE")),
            "can_dump_schema": not schema_issues,
            "can_dump_data": not data_issues,
            "schema_issues": schema_issues,
            "data_issues": data_issues,
            "notes": notes,
        })
    return {
        "engine": "oracle",
        "version": version,
        "user": user,
        "schemas": schemas,
        "notes": [
            "Oracle schemas are users. Output is a SQL*Plus / SQLcl script (run with @file).",
            f"Session has {'a catalog role' if catalog else 'no catalog role'}"
            f"{' and SELECT ANY TABLE' if read_any else ''}.",
        ],
    }


# ---------------------------------------------------------------------------
# Literals
# ---------------------------------------------------------------------------

def _str_pieces(s: str) -> list[str]:
    """Quoted pieces of `s`; newlines become CHR() so no literal spans lines
    (a line holding only '/' would make SQL*Plus execute the buffer)."""
    out: list[str] = []
    for i in range(0, len(s), CHUNK_CHARS):
        piece = s[i : i + CHUNK_CHARS].replace("'", "''")
        piece = piece.replace("\r", "' || CHR(13) || '").replace("\n", "' || CHR(10) || '")
        out.append(f"'{piece}'")
    return out


def _string(s: str, national: bool = False) -> str:
    if s == "":
        return "NULL"  # Oracle stores '' as NULL anyway
    pieces = _str_pieces(s)
    if national:
        pieces = ["N" + p if p.startswith("'") else p for p in pieces]
    return "\n  || ".join(pieces)


def _number(v) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return "BINARY_DOUBLE_NAN"
        if math.isinf(v):
            return "BINARY_DOUBLE_INFINITY" if v > 0 else "-BINARY_DOUBLE_INFINITY"
        return repr(v)
    return str(v)  # Decimal/int; Oracle accepts exponent notation such as 1E+40


class _Column:
    """How to read one column and render its value as an Oracle literal."""

    def __init__(self, name: str, data_type: str, owner_of_type: str | None):
        self.name = name
        t = data_type.upper()
        q = _q(name)
        self.select = q
        self.lob: str | None = None  # "clob" | "blob" for values that may need PL/SQL
        self.supported = True
        if owner_of_type and t not in ("XMLTYPE",):
            self.supported = False
            self.kind = "unsupported"
        elif t in ("NUMBER", "FLOAT", "INTEGER", "BINARY_FLOAT", "BINARY_DOUBLE"):
            self.kind = "number"
        elif t in ("CHAR", "VARCHAR2", "LONG", "ROWID", "UROWID") or t.startswith("VARCHAR"):
            self.kind = "string"
        elif t in ("NCHAR", "NVARCHAR2"):
            self.kind = "nstring"
        elif t in ("CLOB", "NCLOB"):
            self.kind, self.lob = "clob", "clob"
        elif t == "JSON":
            self.kind, self.lob = "clob", "clob"
            self.select = f"JSON_SERIALIZE({q} RETURNING CLOB)"
        elif t == "XMLTYPE":
            self.kind, self.lob = "xml", "clob"
            self.select = f"t.{q}.getClobVal()"
        elif t in ("BLOB",):
            self.kind, self.lob = "blob", "blob"
        elif t == "RAW":
            self.kind = "raw"
        elif t == "DATE":
            self.kind = "date"
            self.select = f"TO_CHAR({q}, 'SYYYY-MM-DD HH24:MI:SS')"
        elif t.startswith("TIMESTAMP") and "LOCAL TIME ZONE" in t:
            self.kind = "timestamp_tz"
            self.select = f"TO_CHAR(CAST({q} AS TIMESTAMP WITH TIME ZONE), 'SYYYY-MM-DD HH24:MI:SS.FF9 TZR')"
        elif t.startswith("TIMESTAMP") and "TIME ZONE" in t:
            self.kind = "timestamp_tz"
            self.select = f"TO_CHAR({q}, 'SYYYY-MM-DD HH24:MI:SS.FF9 TZR')"
        elif t.startswith("TIMESTAMP"):
            self.kind = "timestamp"
            self.select = f"TO_CHAR({q}, 'SYYYY-MM-DD HH24:MI:SS.FF9')"
        elif t.startswith("INTERVAL DAY"):
            self.kind = "interval_ds"
            self.select = f"TO_CHAR({q})"
        elif t.startswith("INTERVAL YEAR"):
            self.kind = "interval_ym"
            self.select = f"TO_CHAR({q})"
        elif t == "BOOLEAN":
            self.kind = "boolean"
        else:  # LONG RAW, BFILE, VECTOR, object types, ...
            self.supported = False
            self.kind = "unsupported"
        self.type_name = data_type

    def literal(self, v) -> str:
        if v is None:
            return "NULL"
        k = self.kind
        if k == "number":
            return _number(v)
        if k == "string":
            return _string(str(v))
        if k == "nstring":
            return _string(str(v), national=True)
        if k in ("clob", "xml"):
            pieces = _str_pieces(v) if v else ["''"]
            body = "\n  || ".join(f"TO_CLOB({p})" for p in pieces)
            return f"XMLTYPE({body})" if k == "xml" else body
        if k in ("blob", "raw"):
            hx = bytes(v).hex().upper()
            if not hx:
                return "EMPTY_BLOB()" if k == "blob" else "NULL"
            parts = [f"'{hx[i:i + 2 * BLOB_CHUNK]}'" for i in range(0, len(hx), 2 * BLOB_CHUNK)]
            return "HEXTORAW(" + "\n  || ".join(parts) + ")"
        if k == "date":
            return f"TO_DATE('{v.strip()}', 'SYYYY-MM-DD HH24:MI:SS')"
        if k == "timestamp":
            return f"TO_TIMESTAMP('{v.strip()}', 'SYYYY-MM-DD HH24:MI:SS.FF9')"
        if k == "timestamp_tz":
            # TZR reads both region names (Europe/Paris) and offsets (+02:00).
            return f"TO_TIMESTAMP_TZ('{v.strip()}', 'SYYYY-MM-DD HH24:MI:SS.FF9 TZR')"
        if k == "interval_ds":
            return f"TO_DSINTERVAL('{v.strip()}')"
        if k == "interval_ym":
            return f"TO_YMINTERVAL('{v.strip()}')"
        if k == "boolean":
            return "TRUE" if v else "FALSE"
        raise DumpError(f"Unsupported value for column {self.name}")

    def needs_plsql(self, v) -> bool:
        if v is None or self.lob is None:
            return False
        if self.lob == "blob":
            return len(v) > 2000  # RAW literal limit
        return len(v) > CHUNK_CHARS * INLINE_LOB_CHUNKS


# ---------------------------------------------------------------------------
# Dump
# ---------------------------------------------------------------------------

def dump(job: DumpJob, writer: DumpWriter, info: dict) -> None:
    req = job.request
    want_schema = req.content in ("schema", "both")
    want_data = req.content in ("data", "both")
    with _connect(req.connection) as conn:
        user, _roles, _privs, prefix = _session(conn)
        cur = conn.cursor()
        cur.execute("""
            BEGIN
              DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', TRUE);
              DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'PRETTY', TRUE);
              DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SEGMENT_ATTRIBUTES', FALSE);
              DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'STORAGE', FALSE);
              DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'REF_CONSTRAINTS', FALSE);
              DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'CONSTRAINTS_AS_ALTER', FALSE);
            END;""")

        writer.line("-- Run with SQL*Plus or SQLcl:  @this_file.sql")
        writer.line("SET DEFINE OFF")
        writer.line("SET SQLBLANKLINES ON")
        writer.line()

        plan = []
        for schema in req.schemas:
            tables = [r for (r,) in cur.execute(
                f"""SELECT table_name FROM {prefix}_tables t
                    WHERE owner = :o AND nested = 'NO' AND secondary = 'N' AND table_name NOT LIKE 'BIN$%'
                      AND (iot_type IS NULL OR iot_type = 'IOT')
                      AND NOT EXISTS (SELECT 1 FROM {prefix}_mviews m WHERE m.owner = t.owner AND m.mview_name = t.table_name)
                    ORDER BY table_name""",
                o=schema,
            )]
            plan.append((schema, tables))
        job.items_total = sum(len(t) for _, t in plan)

        def objects(schema: str, *types: str) -> list[str]:
            binds = ", ".join(f":t{i}" for i in range(len(types)))
            return [n for (n,) in cur.execute(
                f"""SELECT object_name FROM {prefix}_objects
                    WHERE owner = :o AND object_type IN ({binds}) AND generated = 'N'
                      AND object_name NOT LIKE 'BIN$%' AND object_name NOT LIKE 'ISEQ$$%'
                    ORDER BY object_id""",
                {"o": schema, **{f"t{i}": t for i, t in enumerate(types)}},
            ).fetchall()]

        session_schema: list[str | None] = [None]

        def use(schema: str) -> None:
            """Unqualified names inside DDL (e.g. a trigger's `ON orders`) resolve in the
            session's current schema, not the object owner's, so follow the owner."""
            if session_schema[0] != schema:
                writer.line(f"ALTER SESSION SET CURRENT_SCHEMA = {_q(schema)};")
                writer.line()
                session_schema[0] = schema

        def banner(title: str) -> None:
            writer.line(f"-- {'=' * 70}")
            writer.line(f"-- {title}")
            writer.line(f"-- {'=' * 70}")
            writer.line()

        # Phases run across all selected schemas, so cross-schema foreign keys, views
        # and code restore cleanly whatever the schema order.
        if req.include_create_schema:
            banner("Schema owners")
            for schema, _ in plan:
                _create_user(cur, writer, schema, user, prefix)

        if want_schema:
            job.phase = "creating tables"
            for schema, tables in plan:
                banner(f"Types, sequences and tables of {schema}")
                use(schema)
                for name in objects(schema, "TYPE"):
                    _ddl(cur, writer, job, "TYPE_SPEC", name, schema)
                for name in objects(schema, "SEQUENCE"):
                    _ddl(cur, writer, job, "SEQUENCE", name, schema)
                for t in tables:
                    job.check_cancel()
                    job.current = f"{schema}.{t}"
                    _ddl(cur, writer, job, "TABLE", t, schema)
                    _dependent(cur, writer, job, "COMMENT", t, schema)

        if want_data:
            job.phase = "dumping data"
            data_only = req.content == "data"
            fks = [(schema, t, c) for schema, _ in plan for t, c in _foreign_keys(cur, prefix, schema)] if data_only else []
            if data_only:
                # Target tables already exist with their constraints and triggers.
                banner("Disable foreign keys and triggers for the load")
                for schema, t, c in fks:
                    writer.line(f"ALTER TABLE {_q(schema)}.{_q(t)} DISABLE CONSTRAINT {_q(c)};")
                for schema, tables in plan:
                    for t in tables:
                        writer.line(f"ALTER TABLE {_q(schema)}.{_q(t)} DISABLE ALL TRIGGERS;")
                writer.line()
            for schema, tables in plan:
                banner(f"Data of {schema}")
                use(schema)
                identities = _identities(cur, prefix, schema)
                for t in tables:
                    job.check_cancel()
                    job.current = f"{schema}.{t}"
                    _table_data(conn, writer, job, prefix, schema, t, identities.get(t, []))
                    job.items_done += 1
            if data_only:
                banner("Re-enable foreign keys and triggers")
                for schema, tables in plan:
                    for t in tables:
                        writer.line(f"ALTER TABLE {_q(schema)}.{_q(t)} ENABLE ALL TRIGGERS;")
                for schema, t, c in fks:
                    writer.line(f"ALTER TABLE {_q(schema)}.{_q(t)} ENABLE CONSTRAINT {_q(c)};")
                writer.line()
        else:
            job.items_done = job.items_total

        if want_schema:
            job.phase = "indexes, keys and code"
            banner("Indexes")
            for schema, _ in plan:
                use(schema)
                for (name,) in cur.execute(
                    f"""SELECT i.index_name FROM {prefix}_indexes i
                        WHERE i.owner = :o AND i.table_owner = :o AND i.generated = 'N'
                          AND i.index_type NOT IN ('LOB', 'IOT - TOP', 'CLUSTER')
                          AND i.index_name NOT LIKE 'BIN$%'
                          AND NOT EXISTS (SELECT 1 FROM {prefix}_constraints c
                                          WHERE c.owner = i.table_owner AND c.index_name = i.index_name)
                          AND NOT EXISTS (SELECT 1 FROM {prefix}_mviews m
                                          WHERE m.owner = i.table_owner AND m.mview_name = i.table_name)
                        ORDER BY i.table_name, i.index_name""",
                    o=schema,
                ).fetchall():
                    _ddl(cur, writer, job, "INDEX", name, schema)
            # Object grants on tables come before the foreign keys: a cross-schema FK needs
            # the REFERENCES grant. Grants on views and code follow once those exist.
            banner("Object grants on tables")
            for schema, _ in plan:
                use(schema)
                for name in _granted_objects(cur, prefix, schema, tables_only=True):
                    _dependent(cur, writer, job, "OBJECT_GRANT", name, schema)
            banner("Foreign keys")
            for schema, tables in plan:
                use(schema)
                for t in tables:
                    _dependent(cur, writer, job, "REF_CONSTRAINT", t, schema)
            banner("Views, materialized views and PL/SQL")
            for schema, _ in plan:
                use(schema)
                for name in objects(schema, "VIEW"):
                    _ddl(cur, writer, job, "VIEW", name, schema)
                for name in objects(schema, "MATERIALIZED VIEW"):
                    _ddl(cur, writer, job, "MATERIALIZED_VIEW", name, schema)
                for otype, ddl_type in PLSQL_TYPES:
                    for name in objects(schema, otype):
                        _ddl(cur, writer, job, ddl_type, name, schema)
            # Triggers last, so they never fire while the data above is loaded.
            banner("Triggers")
            for schema, _ in plan:
                use(schema)
                for name in objects(schema, "TRIGGER"):
                    _ddl(cur, writer, job, "TRIGGER", name, schema)
            banner("Object grants on views and code")
            for schema, _ in plan:
                use(schema)
                for name in _granted_objects(cur, prefix, schema, tables_only=False):
                    _dependent(cur, writer, job, "OBJECT_GRANT", name, schema)


def _create_user(cur, writer: DumpWriter, schema: str, user: str, prefix: str) -> None:
    tablespace = "USERS"
    try:
        src = "user_users" if schema == user else f"{prefix}_users"
        row = cur.execute(f"SELECT default_tablespace FROM {src} WHERE username = :u", u=schema).fetchone()
        if row and row[0]:
            tablespace = row[0]
    except oracledb.DatabaseError:
        pass
    writer.line("-- Schema owner. Oracle schemas are users: set a password, quota and grants as needed.")
    writer.line(f"CREATE USER {_q(schema)} NO AUTHENTICATION DEFAULT TABLESPACE {_q(tablespace)};")
    writer.line(f"ALTER USER {_q(schema)} QUOTA UNLIMITED ON {_q(tablespace)};")
    writer.line()


def _ddl(cur, writer: DumpWriter, job: DumpJob, ddl_type: str, name: str, owner: str) -> None:
    try:
        text = cur.execute("SELECT DBMS_METADATA.GET_DDL(:t, :n, :o) FROM dual", t=ddl_type, n=name, o=owner).fetchone()[0]
    except oracledb.DatabaseError as e:
        job.log("warning", f"{owner}.{name} ({ddl_type}) skipped: {_err(e)}")
        writer.line(f"-- skipped {ddl_type} {owner}.{name}: {_err(e)}")
        return
    writer.line(text.strip())
    writer.line()


def _dependent(cur, writer: DumpWriter, job: DumpJob, ddl_type: str, table: str, owner: str) -> None:
    try:
        text = cur.execute(
            "SELECT DBMS_METADATA.GET_DEPENDENT_DDL(:t, :n, :o) FROM dual", t=ddl_type, n=table, o=owner
        ).fetchone()[0]
    except oracledb.DatabaseError as e:
        if e.args and getattr(e.args[0], "code", None) == NO_DDL:
            return
        job.log("warning", f"{ddl_type} for {owner}.{table} skipped: {_err(e)}")
        return
    writer.line(text.strip())
    writer.line()


def _granted_objects(cur, prefix: str, schema: str, tables_only: bool) -> list[str]:
    """Objects of `schema` that carry object grants (tables, or everything else)."""
    privs, owner_col = ("dba_tab_privs", "owner") if prefix == "DBA" else ("all_tab_privs", "table_schema")
    op = "=" if tables_only else "<>"
    return [n for (n,) in cur.execute(
        f"""SELECT DISTINCT p.table_name FROM {privs} p
            JOIN {prefix}_objects o ON o.owner = p.{owner_col} AND o.object_name = p.table_name
            WHERE p.{owner_col} = :o AND o.object_type {op} 'TABLE' AND o.object_type NOT IN ('INDEX', 'LOB')
            ORDER BY p.table_name""",
        o=schema,
    ).fetchall()]


def _foreign_keys(cur, prefix: str, schema: str) -> list[tuple[str, str]]:
    return cur.execute(
        f"SELECT table_name, constraint_name FROM {prefix}_constraints "
        "WHERE owner = :o AND constraint_type = 'R' AND status = 'ENABLED' ORDER BY table_name, constraint_name",
        o=schema,
    ).fetchall()


def _identities(cur, prefix: str, schema: str) -> dict[str, list[tuple[str, str]]]:
    """table -> [(column, generation clause)] for identity columns."""
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        rows = cur.execute(
            f"""SELECT i.table_name, i.column_name, i.generation_type, c.default_on_null
                FROM {prefix}_tab_identity_cols i
                JOIN {prefix}_tab_cols c ON c.owner = i.owner AND c.table_name = i.table_name AND c.column_name = i.column_name
                WHERE i.owner = :o""",
            o=schema,
        ).fetchall()
    except oracledb.DatabaseError:
        return out
    for table, column, generation, on_null in rows:
        clause = "ALWAYS" if generation == "ALWAYS" else "BY DEFAULT ON NULL" if on_null == "YES" else "BY DEFAULT"
        out.setdefault(table, []).append((column, clause))
    return out


def _table_data(conn, writer: DumpWriter, job: DumpJob, prefix: str, schema: str, table: str, identities) -> None:
    cur = conn.cursor()
    cols = [
        _Column(name, dtype, dtype_owner)
        for name, dtype, dtype_owner in cur.execute(
            f"""SELECT column_name, data_type, data_type_owner FROM {prefix}_tab_cols
                WHERE owner = :o AND table_name = :t AND hidden_column = 'NO' AND virtual_column = 'NO'
                ORDER BY column_id""",
            o=schema, t=table,
        )
    ]
    unsupported = [c for c in cols if not c.supported]
    qualified = f"{_q(schema)}.{_q(table)}"
    if unsupported:
        msg = ", ".join(f"{c.name} ({c.type_name})" for c in unsupported)
        job.log("warning", f"Data of {schema}.{table} skipped: unsupported column type(s) {msg}")
        writer.line(f"-- data of {qualified} skipped: unsupported column type(s) {msg}")
        writer.line()
        return

    writer.line(f"-- Data for {qualified}")
    for column, _clause in identities:
        if _clause == "ALWAYS":  # explicit values are rejected by GENERATED ALWAYS
            writer.line(f"ALTER TABLE {qualified} MODIFY ({_q(column)} GENERATED BY DEFAULT AS IDENTITY);")
    col_list = ", ".join(_q(c.name) for c in cols)
    select = f"SELECT {', '.join(c.select for c in cols)} FROM {qualified} t"
    data_cur = conn.cursor()
    data_cur.arraysize = 500
    data_cur.prefetchrows = 501
    data_cur.execute(select)
    n = 0
    while True:
        rows = data_cur.fetchmany()
        if not rows:
            break
        for row in rows:
            if any(c.needs_plsql(v) for c, v in zip(cols, row)):
                _insert_plsql(writer, qualified, cols, row)
            else:
                values = [c.literal(v) for c, v in zip(cols, row)]
                stmt = f"INSERT INTO {qualified} ({col_list}) VALUES ({', '.join(values)});"
                if len(stmt) > 2000 or "\n" in stmt:
                    stmt = (
                        f"INSERT INTO {qualified} (\n  " + ",\n  ".join(_q(c.name) for c in cols)
                        + "\n) VALUES (\n  " + ",\n  ".join(values) + "\n);"
                    )
                writer.line(stmt)
            n += 1
            job.rows += 1
            if n % ROWS_PER_COMMIT == 0:
                writer.line("COMMIT;")
        job.check_cancel()
    writer.line("COMMIT;")
    for column, clause in identities:
        # Continue the sequence after the highest loaded value.
        writer.line(f"ALTER TABLE {qualified} MODIFY ({_q(column)} GENERATED {clause} AS IDENTITY (START WITH LIMIT VALUE));")
    writer.line()
    job.log("info", f"{schema}.{table}: {n:,} row(s)")


def _insert_plsql(writer: DumpWriter, qualified: str, cols: list[_Column], row) -> None:
    """Large LOBs: insert an empty LOB, then append the value in pieces."""
    declare, values, returning, appends = [], [], [], []
    for i, (c, v) in enumerate(zip(cols, row)):
        if c.needs_plsql(v):
            var = f"l{i}"
            declare.append(f"  {var} {'BLOB' if c.lob == 'blob' else 'CLOB'};")
            values.append("EMPTY_BLOB()" if c.lob == "blob" else "EMPTY_CLOB()")
            returning.append((_q(c.name), var))
            if c.lob == "blob":
                hx = bytes(v).hex().upper()
                for j in range(0, len(hx), 2 * BLOB_CHUNK):
                    piece = hx[j : j + 2 * BLOB_CHUNK]
                    appends.append(f"  DBMS_LOB.WRITEAPPEND({var}, {len(piece) // 2}, HEXTORAW('{piece}'));")
            else:
                text = v if isinstance(v, str) else str(v)
                for piece in _str_pieces(text):
                    appends.append(f"  DBMS_LOB.APPEND({var}, TO_CLOB({piece}));")
        else:
            values.append(c.literal(v))
    writer.line("DECLARE")
    for d in declare:
        writer.line(d)
    writer.line("BEGIN")
    writer.line(f"  INSERT INTO {qualified} (" + ", ".join(_q(c.name) for c in cols) + ")")
    writer.line("  VALUES (" + ",\n    ".join(values) + ")")
    writer.line("  RETURNING " + ", ".join(r[0] for r in returning) + " INTO " + ", ".join(r[1] for r in returning) + ";")
    for a in appends:
        writer.line(a)
    writer.line("END;")
    writer.line("/")


def _err(e: Exception) -> str:
    return str(e).strip().splitlines()[0]
