import decimal
import os

import pytest

from app.dumper import base
from app.dumper.base import DumpError, resolve_output
from app.dumper.mssql import _literal as ms_literal
from app.dumper.mssql import _type_sql
from app.dumper.mysql import _like, _value
from app.dumper.oracle import _Column, _string


@pytest.fixture
def dump_root(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "DUMP_ROOT", tmp_path)
    return tmp_path


def test_output_path_stays_inside_root(dump_root):
    assert resolve_output("shop/shop.sql") == dump_root / "shop" / "shop.sql"
    assert resolve_output("a\\b.sql") == dump_root / "a" / "b.sql"
    for bad in ("../x.sql", "a/../../x.sql", "/etc/passwd", "C:/x.sql", "", "dir/", "x.sql.partial"):
        with pytest.raises(DumpError):
            resolve_output(bad)


def test_output_path_rejects_symlink_escape(dump_root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    os.symlink(outside, dump_root / "link")
    with pytest.raises(DumpError):
        resolve_output("link/x.sql")


def test_mysql_grant_patterns():
    assert _like("shop", "shop")
    assert _like("shop%", "shop_eu")
    assert _like("sho_", "shop")
    assert not _like("shop\\_eu", "shopxeu")  # escaped underscore is literal
    assert _like("shop\\_eu", "shop_eu")
    assert not _like("shop", "shopping")


def test_mysql_values():
    from pymysql.constants import FIELD_TYPE

    assert _value(None, FIELD_TYPE.VAR_STRING, 45) == "NULL"
    assert _value("it's\n", FIELD_TYPE.VAR_STRING, 45) == "'it\\'s\\n'"
    assert _value("0000-00-00 00:00:00", FIELD_TYPE.DATETIME, 63) == "'0000-00-00 00:00:00'"
    assert _value("129.990", FIELD_TYPE.NEWDECIMAL, 63) == "129.990"  # server text, unquoted
    assert _value(b"\x89PNG", FIELD_TYPE.BLOB, 63) == "0x89504E47"
    assert _value(b"", FIELD_TYPE.BLOB, 63) == "''"
    assert _value(b"\x05", FIELD_TYPE.BIT, 63) == "b'101'"


def test_oracle_strings_never_span_lines():
    s = _string("a'b\n/\nc")
    assert "\n" not in s.replace("\n  || ", "")  # only our own continuation breaks
    assert s == "'a''b' || CHR(10) || '/' || CHR(10) || 'c'"
    assert _string("") == "NULL"
    assert _string("x", national=True) == "N'x'"
    long = _string("x" * 1200)
    assert long.count("||") == 2 and all(len(line) < 600 for line in long.splitlines())


def test_oracle_column_literals():
    assert _Column("N", "NUMBER", None).literal(decimal.Decimal("1234.50")) == "1234.50"
    assert _Column("F", "BINARY_DOUBLE", None).literal(float("inf")) == "BINARY_DOUBLE_INFINITY"
    assert _Column("D", "DATE", None).literal(" 2024-01-02 03:04:05") == "TO_DATE('2024-01-02 03:04:05', 'SYYYY-MM-DD HH24:MI:SS')"
    assert _Column("R", "RAW", None).literal(b"\x00\xff") == "HEXTORAW('00FF')"
    assert _Column("C", "CLOB", None).literal("hi") == "TO_CLOB('hi')"
    blob = _Column("B", "BLOB", None)
    assert blob.needs_plsql(b"x" * 2001) and not blob.needs_plsql(b"x" * 2000)
    assert not _Column("U", "SDO_GEOMETRY", "MDSYS").supported


def test_mssql_types_and_literals():
    assert _type_sql("nvarchar", 382, 0, 0, False, "") == "nvarchar(191)"
    assert _type_sql("varbinary", -1, 0, 0, False, "") == "varbinary(max)"
    assert _type_sql("decimal", 9, 12, 2, False, "") == "decimal(12, 2)"
    assert _type_sql("datetime2", 7, 23, 3, False, "") == "datetime2(3)"
    assert _type_sql("email_t", 382, 0, 0, True, "sales") == "[sales].[email_t]"
    assert ms_literal({"base": "nvarchar", "name": "x"}, "it's") == "N'it''s'"
    assert ms_literal({"base": "bit", "name": "x"}, True) == "1"
    assert ms_literal({"base": "varbinary", "name": "x"}, b"\x00\xff") == "0x00FF"
    assert ms_literal({"base": "geography", "name": "x"}, "4326|POINT (1 2)") == "geography::STGeomFromText(N'POINT (1 2)', 4326)"


def test_mssql_literals_never_contain_a_line_break():
    from app.dumper.mssql import _n

    assert _n("It's\nGO\ndone") == "N'It''s' + NCHAR(10) + N'GO' + NCHAR(10) + N'done'"
    assert _n("a\r\nb") == "N'a' + NCHAR(13) + NCHAR(10) + N'b'"
    assert _n("\n") == "NCHAR(10)"
    long = _n(("x" * 100 + "\n") * 50)
    assert long.startswith("CAST(N'' AS nvarchar(max)) + ") and "\n" not in long
