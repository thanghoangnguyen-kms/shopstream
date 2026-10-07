"""The canonical encoder refuses what would make two machines disagree (HASH-01, CORE-04).

A float, a naive or non-UTC datetime, a Decimal off exponent -2 and any type it doesn't know
all raise CanonError, and the message names the type and path, never the value.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from shopstream_generator import canon
from shopstream_generator.canon import CanonError
from shopstream_generator.ops import Op, OpKind, Table

PLANTED = "PLANTEDVALUE"

REFUSED = [
    pytest.param(12345.6789, "12345", id="float"),
    pytest.param(datetime(2031, 5, 6, 7, 8, 9), "2031", id="naive-datetime"),
    pytest.param(
        datetime(2032, 5, 6, 7, 8, 9, tzinfo=timezone(timedelta(hours=7))),
        "2032",
        id="non-utc-datetime",
    ),
    pytest.param(Decimal("12.3"), "12.3", id="decimal-scale-1"),
    pytest.param(Decimal("1E+2"), "1E+2", id="decimal-exponent"),
    pytest.param(Decimal("NaN"), "NaN", id="decimal-nan"),
    pytest.param({PLANTED}, PLANTED, id="set"),
    pytest.param(PLANTED.encode(), PLANTED, id="bytes"),
    pytest.param(date(2033, 1, 2), "2033", id="date"),
]


def op(row: dict[str, object] | None, kind: OpKind = OpKind.INSERT) -> Op:
    return Op(Table.PRODUCTS, kind, {"product_id": 1}, row)


@pytest.mark.parametrize(("value", "planted"), REFUSED)
def test_an_unsafe_value_raises_and_the_message_omits_it(value: object, planted: str) -> None:
    with pytest.raises(CanonError) as caught:
        canon.encode(value)
    assert planted not in str(caught.value)


def test_the_error_names_the_type_and_the_path() -> None:
    with pytest.raises(CanonError, match=r"row\.list_price: unsupported value of type float"):
        canon.line(1, op({"list_price": 1.5}))


def test_the_error_names_the_path_through_a_list() -> None:
    with pytest.raises(CanonError, match=r"row\.tags\[1\]"):
        canon.line(1, op({"tags": ["a", 2.5]}))


def test_a_bool_stays_a_json_bool_and_an_int_stays_an_integer() -> None:
    assert canon.dumps({"a": True, "b": False, "n": 1}) == '{"a":true,"b":false,"n":1}'
    assert canon.encode(True) is True
    assert canon.encode(1) == 1


def test_none_becomes_null() -> None:
    assert canon.dumps({"v": None}) == '{"v":null}'


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("0.00"), '"0.00"'),
        (Decimal("12.00"), '"12.00"'),
        (Decimal("-3.50"), '"-3.50"'),
        (Decimal("1000000.01"), '"1000000.01"'),
    ],
)
def test_money_is_a_fixed_point_string_at_scale_two(value: Decimal, text: str) -> None:
    assert canon.dumps(value) == text


def test_a_zero_microsecond_timestamp_keeps_its_six_digits() -> None:
    assert canon.dumps(datetime(2025, 7, 1, tzinfo=UTC)) == '"2025-07-01T00:00:00.000000Z"'


def test_a_zero_offset_zone_renders_as_utc() -> None:
    value = datetime(2025, 7, 1, 12, 0, 0, 5, tzinfo=timezone(timedelta(0)))
    assert canon.dumps(value) == '"2025-07-01T12:00:00.000005Z"'


def test_a_non_ascii_string_is_utf8_not_a_backslash_escape() -> None:
    data = canon.line(1, op({"name": "Nguyễn Thị Hồng"}))
    assert "Nguyễn Thị Hồng".encode() in data
    assert b"\\u" not in data


def test_keys_come_out_sorted_by_code_point() -> None:
    assert canon.dumps({"b": 1, "a": {"z": 1, "Z": 2}}) == '{"a":{"Z":2,"z":1},"b":1}'


def test_a_line_holds_one_operation_and_ends_with_exactly_one_newline() -> None:
    data = canon.line(7, op({"product_id": 1, "name": "x"}))
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1
    assert json.loads(data) == {
        "seq": 7,
        "op": "insert",
        "key": {"product_id": 1},
        "row": {"product_id": 1, "name": "x"},
    }
    assert data.startswith(b'{"key":{"product_id":1},"op":"insert","row":')


def test_a_delete_line_has_no_row() -> None:
    data = canon.line(9, op(None, OpKind.DELETE))
    assert set(json.loads(data)) == {"seq", "op", "key"}
    assert b'"row"' not in data


def test_an_op_with_a_row_on_a_delete_or_none_on_an_insert_is_refused() -> None:
    with pytest.raises(ValueError, match="row"):
        op({"product_id": 1}, OpKind.DELETE)
    with pytest.raises(ValueError, match="row"):
        op(None, OpKind.INSERT)


def test_a_dict_with_a_non_string_key_is_refused() -> None:
    with pytest.raises(CanonError, match="key of type int"):
        canon.encode({1: "a"})
