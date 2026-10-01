"""Row hashes that compare across engines: the GATE-05 helper.

ADR-001's go criterion 2 says Spark, PyIceberg, Polars and DuckDB must read the same snapshot
with "the same row hashes". Their Arrow output differs (Spark returns int32 for INT, and
VARIANT is a binary struct in Spark and not exportable in DuckDB), so the hash is computed on
plain Python values, the rows `pyarrow.Table.to_pylist()` returns, and never on Arrow types.

Callers: spark_v3_job.py and read_v3.py in the spike image (Plans 02-04 and 02-05) and
tests/test_row_hash.py. The module imports only the standard library, so CI, which installs no
spike group, unit-tests it with plain dicts, datetime and Decimal values.

Encoding. Every cell is `tag + 8-byte big-endian length + payload`, so neighbouring cells never
merge and no two kinds collide. Tags follow the logical kind, never the Arrow width:

- NULL: a sentinel tag with an empty payload, different from "", "null" and JSON null.
- integer: decimal text. boolean: `true` or `false` (checked before integer). string: the UTF-8
  bytes of the code points as given, with no Unicode normalisation. bytes: the raw bytes.
- float: `float.hex()`, so -0.0 differs from 0.0. decimal: `format(d, "f")` at the value's own
  scale, never `str()` (which can print `1E+2`); NaN and infinities are refused.
- timestamp: UTC microseconds since the epoch as decimal text. An aware datetime is converted to
  UTC and a naive one is read as UTC. date: ISO text.
- JSON column: canonical JSON, whatever the engine's encoding. The caller names the JSON
  columns; the value may be JSON text or an already parsed object.

An unsupported value raises UnsupportedValueError naming the column and the type, never the
value, and nothing is ever hashed through repr() or str(), so two engines cannot agree by
accident. Engines must cast VARIANT to JSON text in SQL first (`to_json(payload)` in Spark,
`payload::JSON` in DuckDB), because raw VARIANT Arrow output is not canonical across engines.

A row digest is the sha256 of its cells in the caller's column order. A table digest is the row
count plus the sha256 of the sorted row digests, so it ignores row order and keeps duplicates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol


class UnsupportedValueError(TypeError):
    """A cell holds a value this helper refuses to hash; the message never includes the value."""


class InvalidJSONError(ValueError):
    """A JSON column holds text that is not valid JSON; the message never includes the text."""


class SupportsToPylist(Protocol):
    """What `arrow_table_digest` needs from a `pyarrow.Table`, without importing pyarrow."""

    def to_pylist(self) -> list[dict[str, object]]: ...


NULL_SENTINEL: bytes = b"\x00"
_INTEGER = b"\x01"
_BOOLEAN = b"\x02"
_STRING = b"\x03"
_BYTES = b"\x04"
_FLOAT = b"\x05"
_DECIMAL = b"\x06"
_TIMESTAMP = b"\x07"
_DATE = b"\x08"
_JSON = b"\x09"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def canonical_json(value: object, *, column: str = "") -> str:
    """Parse JSON text (or take a parsed object) and write it back with sorted keys."""
    parsed = _parse(value, column) if isinstance(value, str) else value
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse(text: str, column: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise InvalidJSONError(f"column {column!r}: invalid JSON text") from None


def _cell(tag: bytes, payload: bytes) -> bytes:
    return tag + len(payload).to_bytes(8, "big") + payload


def _unsupported(column: str, value: object) -> UnsupportedValueError:
    return UnsupportedValueError(
        f"column {column!r}: unsupported value of type {type(value).__name__}"
    )


def encode(value: object, *, json_column: bool, column: str = "") -> bytes:
    """Encode one cell as tag + 8-byte length + payload; `column` only labels errors."""
    if value is None:
        return _cell(NULL_SENTINEL, b"")
    if json_column:
        return _cell(_JSON, canonical_json(value, column=column).encode("utf-8"))
    if isinstance(value, bool):
        return _cell(_BOOLEAN, b"true" if value else b"false")
    if isinstance(value, int):
        return _cell(_INTEGER, str(int(value)).encode("ascii"))
    if isinstance(value, str):
        try:
            return _cell(_STRING, value.encode("utf-8"))
        except UnicodeEncodeError:
            raise _unsupported(column, value) from None
    if isinstance(value, bytes | bytearray | memoryview):
        return _cell(_BYTES, bytes(value))
    if isinstance(value, float):
        return _cell(_FLOAT, float.hex(value).encode("ascii"))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _unsupported(column, value)
        return _cell(_DECIMAL, format(value, "f").encode("ascii"))
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)
        microseconds = (aware - _EPOCH) // _MICROSECOND
        return _cell(_TIMESTAMP, str(microseconds).encode("ascii"))
    if isinstance(value, date):
        return _cell(_DATE, value.isoformat().encode("ascii"))
    raise _unsupported(column, value)


def row_digest(
    row: Mapping[str, object],
    columns: Sequence[str],
    json_columns: Collection[str] = frozenset(),
) -> str:
    """sha256 hex of the row's cells in the caller's column order; extra keys are ignored."""
    cells: list[bytes] = []
    for column in columns:
        if column not in row:
            raise KeyError(column)
        cells.append(encode(row[column], json_column=column in json_columns, column=column))
    return hashlib.sha256(b"".join(cells)).hexdigest()


def table_digest(
    rows: Iterable[Mapping[str, object]],
    columns: Sequence[str],
    json_columns: Collection[str] = frozenset(),
) -> tuple[int, str]:
    """Row count and the sha256 hex of the sorted row digests: order-free, duplicates kept."""
    digests = sorted(row_digest(row, columns, json_columns) for row in rows)
    return len(digests), hashlib.sha256("".join(digests).encode("ascii")).hexdigest()


def arrow_table_digest(
    table: SupportsToPylist,
    columns: Sequence[str],
    json_columns: Collection[str] = frozenset(),
) -> tuple[int, str]:
    """`table_digest` over `table.to_pylist()`, e.g. a `pyarrow.Table` from any engine."""
    return table_digest(table.to_pylist(), columns, json_columns)
