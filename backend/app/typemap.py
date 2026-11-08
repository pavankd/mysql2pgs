"""MySQL -> PostgreSQL column type, default and value conversion."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass

from .introspect import Column

INT_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
TEMPORAL_TYPES = {"date", "datetime", "timestamp", "time", "year"}
TEXT_TYPES = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set", "json"}
BINARY_TYPES = {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"}
SPATIAL_TYPES = {
    "geometry", "point", "linestring", "polygon",
    "multipoint", "multilinestring", "multipolygon", "geometrycollection",
}

Converter = Callable[[object], object]


@dataclass
class MappedColumn:
    pg_type: str
    identity: bool
    default_sql: str | None
    check_sql: str | None
    select_expr: str  # MySQL expression used when reading the column
    converter: Converter | None
    on_update_current_timestamp: bool
    warnings: list[str]


def qi(name: str) -> str:
    """Quote a PostgreSQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def ql(value: str) -> str:
    """Quote a PostgreSQL string literal (standard_conforming_strings=on)."""
    return "'" + value.replace("\x00", "").replace("'", "''") + "'"


def mq(name: str) -> str:
    """Quote a MySQL identifier."""
    return "`" + name.replace("`", "``") + "`"


def parse_enum_values(column_type: str) -> list[str]:
    """Extract the values of ``enum('a','b''c')`` / ``set(...)`` definitions."""
    inner = column_type[column_type.index("(") + 1 : column_type.rindex(")")]
    return [m.replace("''", "'").replace("\\\\", "\\") for m in re.findall(r"'((?:[^']|'')*)'", inner)]


def map_type(col: Column, tinyint1_as_boolean: bool) -> str:
    t = col.data_type
    ct = col.column_type.lower()
    unsigned = "unsigned" in ct
    auto_inc = "auto_increment" in col.extra

    if t == "tinyint":
        if tinyint1_as_boolean and ct.startswith("tinyint(1)") and not auto_inc:
            return "boolean"
        return "smallint"
    if t == "smallint":
        return "integer" if unsigned else "smallint"
    if t == "mediumint":
        return "integer"
    if t in ("int", "integer"):
        return "bigint" if unsigned else "integer"
    if t == "bigint":
        # BIGINT UNSIGNED -> bigint (not numeric(20)) so it stays usable as an identity column
        # and FK-compatible with the keys it references. Values above 2^63-1 make COPY fail
        # loudly with "out of range" rather than being silently altered.
        return "bigint"
    if t in ("decimal", "numeric"):
        return f"numeric({col.num_precision or 10},{col.num_scale or 0})"
    if t == "float":
        return "real"
    if t in ("double", "real"):
        return "double precision"
    if t == "bit":
        n = col.num_precision or 1
        return "boolean" if n == 1 else f"bit({n})"
    if t == "char":
        return f"char({col.char_length or 1})"
    if t == "varchar":
        return f"varchar({col.char_length or 255})"
    if t in ("tinytext", "text", "mediumtext", "longtext", "enum", "set"):
        return "text"
    if t == "json":
        return "jsonb"
    if t in BINARY_TYPES:
        return "bytea"
    if t == "date":
        return "date"
    fsp = col.dt_precision or 0
    if t == "datetime":
        return f"timestamp({fsp})"
    if t == "timestamp":
        return f"timestamptz({fsp})"
    if t == "time":
        return f"time({fsp})"
    if t == "year":
        return "smallint"
    if t in SPATIAL_TYPES:
        return "text"  # stored as WKT; switch to PostGIS geometry after migration if needed
    return "text"


_ZERO_DATE = re.compile(r"^0000-00-00")
_NUMERIC_LITERAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _bit_literal_to_int(d: str) -> int | None:
    m = re.fullmatch(r"b'([01]*)'", d.strip())
    if m:
        return int(m.group(1) or "0", 2)
    if d.strip().isdigit():
        return int(d.strip())
    return None


def map_default(col: Column, pg_type: str) -> tuple[str | None, list[str]]:
    d = col.default
    warnings: list[str] = []
    if d is None or "auto_increment" in col.extra:
        return None, warnings
    t = col.data_type

    if t in TEMPORAL_TYPES and t != "year":
        if d.upper().startswith("CURRENT_TIMESTAMP"):
            return "CURRENT_TIMESTAMP", warnings
        if _ZERO_DATE.match(d):
            warnings.append(f"zero-date default '{d}' on column {col.name} dropped")
            return None, warnings
        return ql(d), warnings
    if pg_type == "boolean":
        n = _bit_literal_to_int(d) if t == "bit" else (int(float(d)) if _NUMERIC_LITERAL.match(d) else None)
        if n is None:
            warnings.append(f"unrecognised boolean default {d!r} on column {col.name} dropped")
            return None, warnings
        return ("true" if n else "false"), warnings
    if t == "bit":
        n = _bit_literal_to_int(d)
        if n is None:
            return None, warnings
        width = col.num_precision or 1
        return f"B'{format(n, f'0{width}b')}'", warnings
    if t in INT_TYPES or t in ("decimal", "numeric", "float", "double", "real", "year"):
        if _NUMERIC_LITERAL.match(d):
            return d, warnings
        warnings.append(f"non-numeric default {d!r} on column {col.name} dropped")
        return None, warnings
    if t in BINARY_TYPES or t in SPATIAL_TYPES:
        if d == "":
            return "''::bytea" if pg_type == "bytea" else None, warnings
        warnings.append(f"default on binary/spatial column {col.name} dropped")
        return None, warnings
    return ql(d), warnings


# ---------------------------------------------------------------------------
# Value converters (run on every row while copying data)
# ---------------------------------------------------------------------------

def _strip_nul(v):
    if isinstance(v, str):
        return v.replace("\x00", "") if "\x00" in v else v
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", errors="replace").replace("\x00", "")
    if isinstance(v, set):
        return ",".join(sorted(v))
    return v


def _int_to_bool(v):
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return any(v)
    return v != 0


def _make_bits(width: int) -> Converter:
    def conv(v):
        if v is None:
            return None
        n = int.from_bytes(v, "big") if isinstance(v, (bytes, bytearray)) else int(v)
        return format(n, f"0{width}b")
    return conv


def _make_temporal(nullable: bool, kind: str) -> Converter:
    # pymysql returns invalid dates such as 0000-00-00 as raw strings.
    replacement = None if nullable else "-infinity"

    def conv(v):
        if isinstance(v, str):
            return replacement
        return v

    def conv_time(v):
        if isinstance(v, dt.timedelta):
            if dt.timedelta(0) <= v < dt.timedelta(days=1):
                return (dt.datetime.min + v).time()
            raise ValueError(f"TIME value {v} is outside PostgreSQL's 00:00-24:00 range")
        return v

    return conv_time if kind == "time" else conv


def map_column(col: Column, target_name: str, tinyint1_as_boolean: bool) -> MappedColumn:
    pg_type = map_type(col, tinyint1_as_boolean)
    default_sql, warnings = map_default(col, pg_type)
    t = col.data_type
    identity = "auto_increment" in col.extra and pg_type in ("smallint", "integer", "bigint")

    check_sql = None
    if t == "enum":
        values = parse_enum_values(col.column_type)
        # MySQL in non-strict mode stores '' for invalid enum values, so allow it.
        allowed = ", ".join(ql(v) for v in dict.fromkeys(values + [""]))
        check_sql = f"CHECK ({qi(target_name)} IN ({allowed}))"

    converter: Converter | None = None
    select_expr = mq(col.name)
    if pg_type == "boolean":
        converter = _int_to_bool
    elif t == "bit":
        converter = _make_bits(col.num_precision or 1)
    elif t in ("date", "datetime", "timestamp", "time"):
        converter = _make_temporal(col.nullable, t)
    elif t in TEXT_TYPES:
        converter = _strip_nul
    elif t in SPATIAL_TYPES:
        select_expr = f"ST_AsText({mq(col.name)})"
        warnings.append(f"spatial column {col.name} migrated as WKT text")

    on_update = "on update current_timestamp" in col.extra
    return MappedColumn(pg_type, identity, default_sql, check_sql, select_expr, converter, on_update, warnings)
