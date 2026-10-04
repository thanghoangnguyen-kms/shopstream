"""Unit tests for the GATE-05 row-hash helper.

The helper is stdlib-only, so every case here uses plain dicts, datetime, date, Decimal, bytes
and float values, the same shapes `pyarrow.Table.to_pylist()` returns. CI installs no spike
group, which is why nothing here imports pyarrow.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
import row_hash

JSON_COLUMN = frozenset({"v"})


def cell(value: object, *, json: bool = False) -> str:
    """Digest of a one-column row, so a test compares exactly one value's encoding."""
    return row_hash.row_digest({"v": value}, ["v"], JSON_COLUMN if json else frozenset())


class FakeArrowTable:
    """The one method of pyarrow.Table that arrow_table_digest uses."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_pylist(self) -> list[dict[str, object]]:
        return self._rows


def test_adjacent_cells_never_merge() -> None:
    columns = ["x", "y"]
    first = row_hash.row_digest({"x": "a", "y": "bc"}, columns)
    second = row_hash.row_digest({"x": "ab", "y": "c"}, columns)
    assert first != second


def test_kinds_never_collide() -> None:
    values: list[object] = [1, True, "1", b"1", Decimal(1), 1.0]
    digests = {cell(value) for value in values}
    assert len(digests) == len(values)


def test_null_empty_string_and_null_text_differ() -> None:
    assert len({cell(None), cell(""), cell("null")}) == 3


def test_row_digest_is_64_lowercase_hex() -> None:
    digest = cell("anything")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_cell_layout_is_tag_length_payload() -> None:
    encoded = row_hash.encode("ab", json_column=False)
    assert encoded[1:9] == (2).to_bytes(8, "big")
    assert encoded[9:] == b"ab"
    assert row_hash.encode(None, json_column=False) == row_hash.NULL_SENTINEL + bytes(8)


def test_every_kind_has_its_own_tag() -> None:
    samples: list[object] = [
        None,
        1,
        True,
        "s",
        b"b",
        1.5,
        Decimal("1.5"),
        datetime(2026, 1, 1, tzinfo=UTC),
        date(2026, 1, 1),
    ]
    tags = [row_hash.encode(sample, json_column=False)[:1] for sample in samples]
    assert len(set(tags)) == len(samples)
    assert row_hash.encode("{}", json_column=True)[:1] not in tags


def test_decimal_exponent_form_hashes_like_plain_form() -> None:
    assert cell(Decimal("1E+2")) == cell(Decimal("100"))
    assert row_hash.encode(Decimal("1E+2"), json_column=False)[9:] == b"100"


def test_decimal_keeps_scale() -> None:
    assert cell(Decimal("1.50")) != cell(Decimal("1.5"))


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_decimal_is_unsupported(bad: str) -> None:
    with pytest.raises(row_hash.UnsupportedValueError):
        cell(Decimal(bad))


def test_aware_timestamps_convert_to_utc() -> None:
    plus_one = datetime(2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=1)))
    utc = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert cell(plus_one) == cell(utc)


def test_naive_timestamp_is_read_as_utc() -> None:
    assert cell(datetime(2026, 1, 1)) == cell(datetime(2026, 1, 1, tzinfo=UTC))


def test_timestamp_keeps_microseconds() -> None:
    base = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    assert cell(base) != cell(base + timedelta(microseconds=1))
    assert cell(base) == cell(datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC))


def test_timestamp_payload_is_utc_microseconds_since_epoch() -> None:
    encoded = row_hash.encode(datetime(1970, 1, 1, 0, 0, 1, 5, tzinfo=UTC), json_column=False)
    assert encoded[9:] == b"1000005"


def test_date_hashes_as_iso_text_and_differs_from_a_midnight_timestamp() -> None:
    assert row_hash.encode(date(2026, 1, 1), json_column=False)[9:] == b"2026-01-01"
    assert cell(date(2026, 1, 1)) != cell(datetime(2026, 1, 1, tzinfo=UTC))


def test_bytes_differ_from_text_and_buffers_match_bytes() -> None:
    assert cell(b"abc") != cell("abc")
    assert cell(bytearray(b"abc")) == cell(b"abc")
    assert cell(memoryview(b"abc")) == cell(b"abc")


def test_float_hashes_through_hex_and_keeps_negative_zero() -> None:
    assert row_hash.encode(0.1, json_column=False)[9:] == (0.1).hex().encode("ascii")
    assert cell(-0.0) != cell(0.0)


def test_no_unicode_normalisation() -> None:
    composed = unicodedata.normalize("NFC", "é")
    decomposed = unicodedata.normalize("NFD", "é")
    assert composed != decomposed
    assert cell(composed) != cell(decomposed)


def test_lone_surrogate_string_is_unsupported() -> None:
    with pytest.raises(row_hash.UnsupportedValueError):
        cell("\ud800")


@pytest.mark.parametrize(
    ("value", "leak"),
    [
        (time(12, 34, 56), "34"),
        (["leaky-secret-value"], "leaky-secret-value"),
        (object(), "object at"),
    ],
)
def test_unsupported_value_names_column_and_type_never_the_value(value: object, leak: str) -> None:
    with pytest.raises(row_hash.UnsupportedValueError) as info:
        row_hash.row_digest({"payload_col": value}, ["payload_col"])
    message = str(info.value)
    assert "payload_col" in message
    assert type(value).__name__ in message
    assert leak not in message
    assert repr(value) not in message


def test_missing_column_raises_key_error_naming_it() -> None:
    with pytest.raises(KeyError, match="absent_col"):
        row_hash.row_digest({"x": 1}, ["x", "absent_col"])


def test_extra_keys_in_the_row_are_ignored() -> None:
    assert row_hash.row_digest({"x": 1, "noise": 9}, ["x"]) == row_hash.row_digest({"x": 1}, ["x"])


def test_column_order_is_the_callers_order() -> None:
    row = {"x": "left", "y": "right"}
    assert row_hash.row_digest(row, ["x", "y"]) != row_hash.row_digest(row, ["y", "x"])
    swapped = {"x": "right", "y": "left"}
    assert row_hash.row_digest(row, ["x", "y"]) != row_hash.row_digest(swapped, ["x", "y"])


def test_empty_table_digest_is_count_zero_and_sha256_of_nothing() -> None:
    assert row_hash.table_digest([], ["x"]) == (0, hashlib.sha256(b"").hexdigest())


def test_table_digest_does_not_depend_on_row_order() -> None:
    first, second, third = ({"x": 1}, {"x": 2}, {"x": 3})
    forward = row_hash.table_digest([first, second, third], ["x"])
    backward = row_hash.table_digest([third, second, first], ["x"])
    assert forward == backward
    assert forward[0] == 3


def test_table_digest_keeps_duplicates() -> None:
    row = {"x": 1}
    once = row_hash.table_digest([row], ["x"])
    twice = row_hash.table_digest([row, row], ["x"])
    assert once[0] == 1
    assert twice[0] == 2
    assert once[1] != twice[1]


def test_table_digest_is_sha256_over_the_sorted_row_digests() -> None:
    rows = [{"x": 1}, {"x": 2}]
    digests = sorted(row_hash.row_digest(row, ["x"]) for row in rows)
    expected = hashlib.sha256("".join(digests).encode("ascii")).hexdigest()
    assert row_hash.table_digest(rows, ["x"]) == (2, expected)


def test_arrow_table_digest_delegates_to_to_pylist() -> None:
    rows: list[dict[str, object]] = [{"x": 1, "y": "a"}, {"x": 2, "y": None}]
    columns = ["x", "y"]
    assert row_hash.arrow_table_digest(FakeArrowTable(rows), columns) == row_hash.table_digest(
        rows, columns
    )


def test_json_column_text_is_independent_of_key_order() -> None:
    assert cell('{"b":1,"a":2}', json=True) == cell('{"a":2,"b":1}', json=True)


def test_json_text_ignores_key_order_and_whitespace() -> None:
    texts = ['{"b":1,"a":2}', '{"a":2,"b":1}', '{ "a" : 2, "b" : 1 }']
    assert len({cell(text, json=True) for text in texts}) == 1


@pytest.mark.parametrize(
    "spellings",
    [
        ["[1]", "[1.0]", "[1.00]", "[1e0]"],
        ["[0.1]", "[0.10]"],
        ["[-0]", "[0]", "[0.0]", "[-0.000]"],
        ["[1e2]", "[100]", "[1.0E+2]"],
    ],
)
def test_json_numbers_with_one_value_hash_alike(spellings: list[str]) -> None:
    assert len({cell(text, json=True) for text in spellings}) == 1


def test_json_integers_stay_exact_beyond_float_range() -> None:
    assert cell("[9007199254740993]", json=True) != cell("[9007199254740992]", json=True)
    long_a = "[12345678901234567890123456789012345]"
    long_b = "[12345678901234567890123456789012346]"
    assert cell(long_a, json=True) != cell(long_b, json=True)
    assert row_hash.canonical_json(long_a) == long_a
    fractional = "[1.000000000000000000000000000001]"
    assert row_hash.canonical_json(fractional) == fractional


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{ "b" : [1, 2.50, null, true] , "a" : "x" }', '{"a":"x","b":[1,2.5,null,true]}'),
        ('{"b":{"y":1,"x":2},"a":1}', '{"a":1,"b":{"x":2,"y":1}}'),
        ("1e2", "100"),
        ("-0.0", "0"),
        ("1E-7", "0.0000001"),
        ('{"k":"\\u00e9"}', '{"k":"é"}'),
        ("[]", "[]"),
    ],
)
def test_canonical_json_text(text: str, expected: str) -> None:
    assert row_hash.canonical_json(text) == expected


def test_parsed_float_equals_the_same_json_number() -> None:
    assert cell({"n": 1.5}, json=True) == cell('{"n":1.5}', json=True)
    assert cell({"n": Decimal("1.50")}, json=True) == cell('{"n":1.5}', json=True)
    assert cell({"n": 2}, json=True) == cell('{"n":2.0}', json=True)


def test_json_true_false_and_null_stay_distinct() -> None:
    assert cell("[true]", json=True) != cell("[1]", json=True)
    assert cell("[false]", json=True) != cell("[0]", json=True)
    assert cell("[null,1]", json=True) != cell("[1]", json=True)
    assert cell([True], json=True) == cell("[true]", json=True)


def test_json_escapes_resolve_on_parse() -> None:
    assert cell('"\\u00e9"', json=True) == cell('"é"', json=True)
    assert cell('"é"', json=True) != cell('"e"', json=True)


def test_one_payload_hashes_alike_in_every_engine_encoding() -> None:
    # Assumption-delta invariant: VARIANT text from Spark, from DuckDB, the JSON-string
    # fallback and a parsed object are one value, so they must be one digest.
    spark_text = '{"k":1,"n":null,"t":["x","y"]}'
    duckdb_text = '{"k":1,"t":["x","y"],"n":null}'
    string_column = '{"t": ["x", "y"], "k": 1, "n": null}'
    parsed = {"k": 1, "n": None, "t": ["x", "y"]}
    columns = ["id", "payload"]
    digests = {
        row_hash.table_digest([{"id": 1, "payload": payload}], columns, {"payload"})
        for payload in (spark_text, duckdb_text, string_column, parsed)
    }
    assert len(digests) == 1


def test_sql_null_and_json_null_differ_in_a_json_column() -> None:
    assert cell(None, json=True) != cell("null", json=True)
    assert cell(None, json=True) == cell(None)


def test_invalid_json_text_names_the_column_only() -> None:
    with pytest.raises(row_hash.InvalidJSONError) as info:
        row_hash.row_digest({"payload_col": "leaky-secret{"}, ["payload_col"], {"payload_col"})
    assert "payload_col" in str(info.value)
    assert "leaky-secret" not in str(info.value)


@pytest.mark.parametrize("text", ["[NaN]", "[Infinity]", "[-Infinity]"])
def test_non_finite_json_number_is_unsupported(text: str) -> None:
    with pytest.raises(row_hash.UnsupportedValueError):
        cell(text, json=True)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), Decimal("NaN")])
def test_non_finite_parsed_number_is_unsupported(bad: object) -> None:
    with pytest.raises(row_hash.UnsupportedValueError):
        cell([bad], json=True)


@pytest.mark.parametrize(
    "bad",
    [{1: "a"}, {"k": {1, 2}}, {"k": (1, 2)}, {"k": datetime(2026, 1, 1, tzinfo=UTC)}, [object()]],
)
def test_other_types_inside_a_parsed_object_are_unsupported(bad: object) -> None:
    with pytest.raises(row_hash.UnsupportedValueError) as info:
        row_hash.row_digest({"payload_col": bad}, ["payload_col"], {"payload_col"})
    assert "payload_col" in str(info.value)


def test_lone_surrogate_escape_in_json_text_is_unsupported() -> None:
    with pytest.raises(row_hash.UnsupportedValueError, match="payload_col"):
        row_hash.row_digest({"payload_col": '"\\ud800"'}, ["payload_col"], {"payload_col"})
