import datetime as dt
import decimal
import uuid

from app.query import _jsonable


def test_jsonable_keeps_precision():
    assert _jsonable(2**53 - 1) == 2**53 - 1
    assert _jsonable(2**63 - 1) == str(2**63 - 1)  # beyond JS safe integers
    assert _jsonable(decimal.Decimal("129.990")) == "129.990"
    assert _jsonable(float("nan")) == "nan"


def test_jsonable_binary_and_structured():
    assert _jsonable(b"\x89PNG") == "\\x89504e47"
    assert _jsonable(memoryview(b"\x01")) == "\\x01"
    assert _jsonable({"a": [1, 2]}) == '{"a": [1, 2]}'
    assert _jsonable(dt.date(2024, 1, 2)) == "2024-01-02"
    u = uuid.uuid4()
    assert _jsonable(u) == str(u)


def test_jsonable_passthrough():
    for v in (None, True, "x", 3, 1.5):
        assert _jsonable(v) == v


def test_mysql_type_names():
    from pymysql.constants import FIELD_TYPE

    from app.query import MYSQL_TYPE_NAMES

    assert MYSQL_TYPE_NAMES[FIELD_TYPE.LONGLONG] == "bigint"
    assert MYSQL_TYPE_NAMES[FIELD_TYPE.NEWDECIMAL] == "decimal"
    assert MYSQL_TYPE_NAMES[FIELD_TYPE.DATETIME] == "datetime"
    assert MYSQL_TYPE_NAMES[FIELD_TYPE.BIT] == "bit"


def test_mysql_text_vs_binary_by_charset():
    from pymysql.constants import FIELD_TYPE

    from app.query import mysql_type_name

    assert mysql_type_name(FIELD_TYPE.BLOB, 45) == "text"  # utf8mb4
    assert mysql_type_name(FIELD_TYPE.BLOB, 63) == "blob"  # binary
    assert mysql_type_name(FIELD_TYPE.VAR_STRING, 63) == "varbinary"
    assert mysql_type_name(FIELD_TYPE.LONGLONG, 63) == "bigint"
    assert mysql_type_name(FIELD_TYPE.BLOB, None) == "blob"


def test_cancel_before_register_is_honoured():
    from app.query import _Registry

    reg = _Registry()
    assert reg.cancel("q1") is False  # not running yet
    fired = []
    reg.register("q1", lambda: fired.append(True))
    assert fired == [True]  # cancelled as soon as it registered
    assert reg.unregister("q1") == "Query cancelled by user"
    assert reg.peek("q1") is None
