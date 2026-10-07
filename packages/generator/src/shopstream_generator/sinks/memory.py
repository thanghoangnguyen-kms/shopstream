"""The Postgres-like oracle sink (D-25, ADR-005 P1, P3, P5).

`MemoryCdcSink.commit(tick)` judges one tick against `schema.SCHEMAS` and applies it only if
every op passes, so a raising tick changes nothing: not a table, not the last seq, not the last
timestamp. Two kinds of check, labelled on the exception so a Postgres-equivalent check is never
weakened to fit:

- `postgres`: Postgres itself would reject it (a duplicate primary key, an update or delete of a
  missing row, a NULL in a NOT NULL column, a column that doesn't exist, a value its type
  refuses, a failed CHECK);
- `oracle`: stricter than Postgres, which would round, pad or accept it silently (a float or a
  wrong-scale value in NUMERIC(18,2), a short CHAR(3), a naive timestamp, a missing column, a
  key that disagrees with its row), or a rule Postgres can't see (ADR-004 C2, C3, C6: the tick
  sequence and clock, `updated_at` and `created_at`, one change per key per tick except the delete
  pair, soft-deleted customers are final, payments are insert-only).

A message names the table, the check, its kind and a column, never a value: generated rows look
personal, and public CI logs are copies erasure can't reach (the `scripts/row_hash.py` rule).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

from .. import clock
from ..ops import Op, OpKind, Table, Tick
from ..schema import SCHEMAS, Column, ColumnType, TableSchema

POSTGRES = "postgres"
ORACLE = "oracle"

Key = tuple[int, ...]
Row = Mapping[str, object]

_INT32 = 2**31
_INT64 = 2**63
_NUMERIC_SCALE = -2
_NUMERIC_DIGITS = 18
_CHAR_LENGTH = 3
_NO_OFFSET = timedelta(0)


class CdcViolation(ValueError):
    """A tick the sink refuses; names the table, the check and its kind, never a row value."""

    def __init__(
        self, table: Table | None, check: str, kind: str, column: str | None = None
    ) -> None:
        self.table = table
        self.check = check
        self.kind = kind
        self.column = column
        where = "tick" if table is None else table.value
        suffix = "" if column is None else f" (column {column})"
        super().__init__(f"{where}: {kind} check {check} failed{suffix}")


def _postgres(table: Table, check: str, column: str | None = None) -> CdcViolation:
    return CdcViolation(table, check, POSTGRES, column)


def _oracle(table: Table | None, check: str, column: str | None = None) -> CdcViolation:
    return CdcViolation(table, check, ORACLE, column)


class _Staging:
    """A tick's changes over the committed tables: read through, applied only on success."""

    def __init__(self, tables: Mapping[Table, Mapping[Key, Row]]) -> None:
        self._tables = tables
        self.changes: dict[tuple[Table, Key], Row | None] = {}

    def get(self, table: Table, key: Key) -> Row | None:
        if (table, key) in self.changes:
            return self.changes[(table, key)]
        return self._tables[table].get(key)

    def put(self, table: Table, key: Key, row: Row) -> None:
        self.changes[(table, key)] = MappingProxyType(dict(row))

    def remove(self, table: Table, key: Key) -> None:
        self.changes[(table, key)] = None


class MemoryCdcSink:
    """The tables a Postgres source would hold, plus the clock rules it can't enforce."""

    def __init__(self) -> None:
        self._tables: dict[Table, dict[Key, Row]] = {table: {} for table in Table}
        self.last_seq = 0
        self.last_ts_us: int | None = None

    def table(self, table: Table) -> Mapping[Key, Row]:
        """A read-only view of one table: primary-key tuple to the row's current after-image."""
        return MappingProxyType(self._tables[table])

    def commit(self, tick: Tick) -> None:
        """Validate the whole tick, then apply it; raise CdcViolation and change nothing."""
        self._check_tick(tick)
        staging = _Staging(self._tables)
        seen: set[tuple[Table, Key]] = set()
        previous: Op | None = None
        for op in tick.ops:
            self._check_op(op, previous, tick, staging, seen)
            previous = op
        for (table, key), row in staging.changes.items():
            if row is None:
                del self._tables[table][key]
            else:
                self._tables[table][key] = row
        self.last_seq = tick.seq
        self.last_ts_us = tick.ts_us

    # ------------------------------------------------------------ tick-level rules

    def _check_tick(self, tick: Tick) -> None:
        if tick.seq != self.last_seq + 1:
            raise _oracle(None, "seq-gap")
        if self.last_ts_us is not None and tick.ts_us <= self.last_ts_us:
            raise _oracle(None, "ts-not-rising")
        if not tick.ops:
            raise _oracle(None, "empty-tick")

    # ------------------------------------------------------------ op-level rules

    def _check_op(
        self,
        op: Op,
        previous: Op | None,
        tick: Tick,
        staging: _Staging,
        seen: set[tuple[Table, Key]],
    ) -> None:
        schema = SCHEMAS[op.table]
        key = _key_of(op, schema)
        paired = (
            op.kind is OpKind.DELETE
            and previous is not None
            and previous.kind is OpKind.UPDATE
            and previous.table is op.table
            and _key_of(previous, schema) == key
        )
        if (op.table, key) in seen and not (paired and schema.delete_pairs):
            raise _oracle(op.table, "key-twice")
        seen.add((op.table, key))
        if schema.insert_only and op.kind is not OpKind.INSERT:
            raise _oracle(op.table, "insert-only")
        if op.kind is OpKind.INSERT:
            self._insert(op, schema, key, tick, staging)
        elif op.kind is OpKind.UPDATE:
            self._update(op, schema, key, tick, staging)
        else:
            self._delete(op, schema, key, paired, staging)

    def _insert(self, op: Op, schema: TableSchema, key: Key, tick: Tick, staging: _Staging) -> None:
        row = _checked_row(op, schema, key)
        if staging.get(op.table, key) is not None:
            raise _postgres(op.table, "duplicate-pk")
        stamp = clock.to_datetime(tick.ts_us)
        if row["created_at"] != stamp:
            raise _oracle(op.table, "created-at-not-tick", "created_at")
        if row["updated_at"] != stamp:
            raise _oracle(op.table, "updated-at-not-tick", "updated_at")
        staging.put(op.table, key, row)

    def _update(self, op: Op, schema: TableSchema, key: Key, tick: Tick, staging: _Staging) -> None:
        row = _checked_row(op, schema, key)
        stored = staging.get(op.table, key)
        if stored is None:
            raise _postgres(op.table, "update-missing")
        _refuse_soft_deleted(op.table, stored)
        if row["created_at"] != stored["created_at"]:
            raise _oracle(op.table, "created-at-changed", "created_at")
        if row["updated_at"] != clock.to_datetime(tick.ts_us):
            raise _oracle(op.table, "updated-at-not-tick", "updated_at")
        staging.put(op.table, key, row)

    def _delete(
        self, op: Op, schema: TableSchema, key: Key, paired: bool, staging: _Staging
    ) -> None:
        if not schema.delete_pairs:
            raise _oracle(op.table, "delete-not-allowed")
        stored = staging.get(op.table, key)
        if stored is None:
            raise _postgres(op.table, "delete-missing")
        _refuse_soft_deleted(op.table, stored)
        if not paired:
            raise _oracle(op.table, "delete-without-update")
        staging.remove(op.table, key)


def _refuse_soft_deleted(table: Table, stored: Row) -> None:
    """A soft-deleted customer never changes again until an erasure (ADR-004 C6, P5)."""
    if table is Table.CUSTOMERS and stored["deleted_at"] is not None:
        raise _oracle(table, "soft-deleted-customer-changed")


def _key_of(op: Op, schema: TableSchema) -> Key:
    """The key as a tuple in primary-key order; its shape must be exactly the primary key's."""
    if set(op.key) != set(schema.primary_key) or any(type(v) is not int for v in op.key.values()):
        raise _oracle(op.table, "key-mismatch")
    return tuple(op.key[name] for name in schema.primary_key)


def _checked_row(op: Op, schema: TableSchema, key: Key) -> Row:
    """The op's row after every column, type and CHECK rule, and its key equal to the row's."""
    row = op.row
    if row is None:  # unreachable: Op refuses a non-delete without a row
        raise _oracle(op.table, "missing-column")
    columns = {column.name: column for column in schema.columns}
    for name in row:
        if name not in columns:
            raise _postgres(op.table, "unknown-column", name)
    for column in schema.columns:
        if column.name not in row:
            raise _oracle(op.table, "missing-column", column.name)
    for column in schema.columns:
        value = row[column.name]
        if value is None:
            if not column.nullable:
                raise _postgres(op.table, "not-null", column.name)
            continue
        _check_type(op.table, column, value)
    for check in schema.checks:
        _check_constraint(op.table, check.column, check.kind, check.low, check.high, row)
    if tuple(row[name] for name in schema.primary_key) != key:
        raise _oracle(op.table, "key-mismatch")
    return row


def _check_type(table: Table, column: Column, value: object) -> None:
    name = column.name
    kind = column.type
    if kind is ColumnType.BIGINT or kind is ColumnType.INTEGER:
        limit = _INT64 if kind is ColumnType.BIGINT else _INT32
        if type(value) is not int or not -limit <= value < limit:
            raise _postgres(table, "wrong-type", name)
    elif kind is ColumnType.TEXT:
        if not isinstance(value, str):
            raise _postgres(table, "wrong-type", name)
    elif kind is ColumnType.CHAR3:
        if not isinstance(value, str):
            raise _postgres(table, "wrong-type", name)
        if len(value) > _CHAR_LENGTH:
            raise _postgres(table, "char3-length", name)
        if len(value) < _CHAR_LENGTH:
            raise _oracle(table, "char3-length", name)
    elif kind is ColumnType.NUMERIC_18_2:
        if type(value) is not Decimal or not value.is_finite():
            raise _oracle(table, "numeric-type", name)
        digits = value.as_tuple()
        if digits.exponent != _NUMERIC_SCALE or len(digits.digits) > _NUMERIC_DIGITS:
            raise _oracle(table, "numeric-scale", name)
    elif type(value) is not datetime or value.utcoffset() != _NO_OFFSET:
        raise _oracle(table, "timestamptz-utc", name)


def _check_constraint(
    table: Table, column: str, kind: str, low: int | None, high: int | None, row: Row
) -> None:
    value = row[column]
    if value is None or not isinstance(value, int | Decimal):
        return
    if kind == "positive":
        failed = value <= 0
    elif kind == "non_negative":
        failed = value < 0
    else:
        failed = low is None or high is None or not low <= value <= high
    if failed:
        raise _postgres(table, f"check-{kind.replace('_', '-')}", column)
