"""Read schema metadata from MySQL's information_schema (5.6 compatible)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import SYSTEM_DATABASES


def _s(v):
    """information_schema values occasionally arrive as bytes; normalise to str."""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    return v


@dataclass
class Column:
    name: str
    position: int
    default: str | None
    nullable: bool
    data_type: str
    char_length: int | None
    num_precision: int | None
    num_scale: int | None
    dt_precision: int | None
    column_type: str
    extra: str
    comment: str


@dataclass
class Index:
    name: str
    unique: bool
    index_type: str
    columns: list[tuple[str, int | None]] = field(default_factory=list)  # (column, prefix length)


@dataclass
class ForeignKey:
    name: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]
    on_update: str
    on_delete: str


@dataclass
class Table:
    name: str
    comment: str
    row_estimate: int
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


@dataclass
class View:
    name: str
    definition: str


@dataclass
class DatabaseInfo:
    name: str
    tables: list[Table]
    views: list[View]
    routine_count: int
    trigger_count: int


def list_databases(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.SCHEMA_NAME AS name,
                   s.DEFAULT_CHARACTER_SET_NAME AS charset,
                   SUM(t.TABLE_TYPE = 'BASE TABLE') AS table_count,
                   SUM(t.TABLE_TYPE = 'VIEW') AS view_count,
                   COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH), 0) AS size_bytes
            FROM information_schema.SCHEMATA s
            LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME
            GROUP BY s.SCHEMA_NAME, s.DEFAULT_CHARACTER_SET_NAME
            ORDER BY s.SCHEMA_NAME
            """
        )
        rows = cur.fetchall()
    return [
        {
            "name": _s(r["name"]),
            "charset": _s(r["charset"]),
            "table_count": int(r["table_count"] or 0),
            "view_count": int(r["view_count"] or 0),
            "size_bytes": int(r["size_bytes"] or 0),
        }
        for r in rows
        if _s(r["name"]) not in SYSTEM_DATABASES
    ]


def list_tables(conn, database: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT TABLE_NAME AS name, TABLE_TYPE AS type, ENGINE AS engine,
                   TABLE_ROWS AS row_estimate,
                   COALESCE(DATA_LENGTH + INDEX_LENGTH, 0) AS size_bytes
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME
            """,
            (database,),
        )
        rows = cur.fetchall()
    return [
        {
            "name": _s(r["name"]),
            "type": "view" if _s(r["type"]) == "VIEW" else "table",
            "engine": _s(r["engine"]),
            "row_estimate": int(r["row_estimate"] or 0),
            "size_bytes": int(r["size_bytes"] or 0),
        }
        for r in rows
    ]


def introspect(conn, database: str, only_tables: set[str] | None = None) -> DatabaseInfo:
    """Collect tables, columns, indexes, foreign keys and views for one database.

    ``only_tables`` restricts both tables and views; ``None`` means everything.
    """

    def wanted(name: str) -> bool:
        return only_tables is None or name in only_tables

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """,
            (database,),
        )
        tables: dict[str, Table] = {}
        for r in cur.fetchall():
            name = _s(r["TABLE_NAME"])
            if wanted(name):
                tables[name] = Table(name, _s(r["TABLE_COMMENT"]) or "", int(r["TABLE_ROWS"] or 0))

        cur.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_DEFAULT, IS_NULLABLE,
                   DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE,
                   DATETIME_PRECISION, COLUMN_TYPE, EXTRA, COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """,
            (database,),
        )
        for r in cur.fetchall():
            t = tables.get(_s(r["TABLE_NAME"]))
            if t is None:
                continue
            t.columns.append(
                Column(
                    name=_s(r["COLUMN_NAME"]),
                    position=int(r["ORDINAL_POSITION"]),
                    default=_s(r["COLUMN_DEFAULT"]),
                    nullable=_s(r["IS_NULLABLE"]) == "YES",
                    data_type=_s(r["DATA_TYPE"]).lower(),
                    char_length=r["CHARACTER_MAXIMUM_LENGTH"],
                    num_precision=r["NUMERIC_PRECISION"],
                    num_scale=r["NUMERIC_SCALE"],
                    dt_precision=r["DATETIME_PRECISION"],
                    column_type=_s(r["COLUMN_TYPE"]),
                    extra=(_s(r["EXTRA"]) or "").lower(),
                    comment=_s(r["COLUMN_COMMENT"]) or "",
                )
            )

        cur.execute(
            """
            SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, INDEX_TYPE
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
            """,
            (database,),
        )
        idx_map: dict[tuple[str, str], Index] = {}
        for r in cur.fetchall():
            tname, iname = _s(r["TABLE_NAME"]), _s(r["INDEX_NAME"])
            t = tables.get(tname)
            if t is None:
                continue
            idx = idx_map.get((tname, iname))
            if idx is None:
                idx = Index(iname, int(r["NON_UNIQUE"]) == 0, _s(r["INDEX_TYPE"]).upper())
                idx_map[(tname, iname)] = idx
                t.indexes.append(idx)
            sub = r["SUB_PART"]
            idx.columns.append((_s(r["COLUMN_NAME"]), int(sub) if sub is not None else None))

        cur.execute(
            """
            SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA,
                   k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.UPDATE_RULE, r.DELETE_RULE
            FROM information_schema.KEY_COLUMN_USAGE k
            JOIN information_schema.REFERENTIAL_CONSTRAINTS r
              ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
             AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
             AND r.TABLE_NAME = k.TABLE_NAME
            WHERE k.TABLE_SCHEMA = %s AND k.REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
            """,
            (database,),
        )
        fk_map: dict[tuple[str, str], ForeignKey] = {}
        for r in cur.fetchall():
            tname, cname = _s(r["TABLE_NAME"]), _s(r["CONSTRAINT_NAME"])
            t = tables.get(tname)
            if t is None:
                continue
            fk = fk_map.get((tname, cname))
            if fk is None:
                fk = ForeignKey(
                    cname, [], _s(r["REFERENCED_TABLE_SCHEMA"]), _s(r["REFERENCED_TABLE_NAME"]), [],
                    _s(r["UPDATE_RULE"]), _s(r["DELETE_RULE"]),
                )
                fk_map[(tname, cname)] = fk
                t.foreign_keys.append(fk)
            fk.columns.append(_s(r["COLUMN_NAME"]))
            fk.ref_columns.append(_s(r["REFERENCED_COLUMN_NAME"]))

        cur.execute(
            "SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
            (database,),
        )
        views = [View(_s(r["TABLE_NAME"]), _s(r["VIEW_DEFINITION"]) or "") for r in cur.fetchall() if wanted(_s(r["TABLE_NAME"]))]

        cur.execute("SELECT COUNT(*) AS n FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s", (database,))
        routine_count = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s", (database,))
        trigger_count = int(cur.fetchone()["n"])

    return DatabaseInfo(database, list(tables.values()), views, routine_count, trigger_count)
