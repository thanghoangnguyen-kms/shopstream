"""The six captured tables as data: column types, nullability, keys, CHECKs and delete rules (D-25).

This is REF section 7 plus `created_at` and `updated_at` on every table, in the shape Phase 4's
parity test reads: the Postgres init SQL there must match this module column for column. That SQL
has no foreign keys (a late product's insert follows its first order line), sets
`REPLICA IDENTITY FULL`, and has no `now()` defaults (ADR-004 C1). `tier` arrives with Phase 3's
schema drift and is not here.

`MemoryCdcSink` judges every op against `SCHEMAS`. `delete_pairs` says a table allows ADR-004
C6's hard delete (an `UPDATE` then a `DELETE` in one tick); `insert_only` says its rows never
change after the insert.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .ops import Table


class ColumnType(StrEnum):
    BIGINT = "bigint"
    INTEGER = "integer"
    TEXT = "text"
    CHAR3 = "char(3)"
    NUMERIC_18_2 = "numeric(18,2)"
    TIMESTAMPTZ = "timestamptz"


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    nullable: bool


@dataclass(frozen=True)
class Check:
    """A CHECK constraint on one column: `positive` (> 0), `non_negative` (>= 0) or `between`."""

    column: str
    kind: str
    low: int | None
    high: int | None


@dataclass(frozen=True)
class TableSchema:
    table: Table
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    checks: tuple[Check, ...]
    delete_pairs: bool
    insert_only: bool


def _col(name: str, type_: ColumnType, nullable: bool = False) -> Column:
    return Column(name, type_, nullable)


_STAMPS = (
    _col("created_at", ColumnType.TIMESTAMPTZ),
    _col("updated_at", ColumnType.TIMESTAMPTZ),
)


def _table(
    table: Table,
    columns: tuple[Column, ...],
    primary_key: tuple[str, ...],
    checks: tuple[Check, ...] = (),
    *,
    delete_pairs: bool = False,
    insert_only: bool = False,
) -> TableSchema:
    return TableSchema(table, (*columns, *_STAMPS), primary_key, checks, delete_pairs, insert_only)


SCHEMAS: Mapping[Table, TableSchema] = {
    Table.CUSTOMERS: _table(
        Table.CUSTOMERS,
        (
            _col("customer_id", ColumnType.BIGINT),
            _col("email", ColumnType.TEXT),
            _col("full_name", ColumnType.TEXT),
            _col("city", ColumnType.TEXT),
            _col("country", ColumnType.TEXT),
            _col("deleted_at", ColumnType.TIMESTAMPTZ, nullable=True),
        ),
        ("customer_id",),
    ),
    Table.PRODUCTS: _table(
        Table.PRODUCTS,
        (
            _col("product_id", ColumnType.BIGINT),
            _col("name", ColumnType.TEXT),
            _col("category", ColumnType.TEXT),
            _col("list_price", ColumnType.NUMERIC_18_2),
            _col("deleted_at", ColumnType.TIMESTAMPTZ, nullable=True),
        ),
        ("product_id",),
    ),
    Table.ORDERS: _table(
        Table.ORDERS,
        (
            _col("order_id", ColumnType.BIGINT),
            _col("customer_id", ColumnType.BIGINT),
            _col("status", ColumnType.TEXT),
            _col("currency_code", ColumnType.CHAR3),
            _col("order_discount", ColumnType.NUMERIC_18_2),
            _col("ordered_at", ColumnType.TIMESTAMPTZ),
        ),
        ("order_id",),
        (Check("order_discount", "non_negative", None, None),),
    ),
    Table.ORDER_ITEMS: _table(
        Table.ORDER_ITEMS,
        (
            _col("order_id", ColumnType.BIGINT),
            _col("line_number", ColumnType.INTEGER),
            _col("product_id", ColumnType.BIGINT),
            _col("quantity", ColumnType.INTEGER),
            _col("unit_price", ColumnType.NUMERIC_18_2),
        ),
        ("order_id", "line_number"),
        delete_pairs=True,
    ),
    Table.PAYMENTS: _table(
        Table.PAYMENTS,
        (
            _col("payment_id", ColumnType.BIGINT),
            _col("order_id", ColumnType.BIGINT),
            _col("payment_kind", ColumnType.TEXT),
            _col("amount", ColumnType.NUMERIC_18_2),
            _col("currency_code", ColumnType.CHAR3),
            _col("payment_method", ColumnType.TEXT),
            _col("paid_at", ColumnType.TIMESTAMPTZ),
        ),
        ("payment_id",),
        (Check("amount", "positive", None, None),),
        insert_only=True,
    ),
    Table.REVIEWS: _table(
        Table.REVIEWS,
        (
            _col("review_id", ColumnType.BIGINT),
            _col("product_id", ColumnType.BIGINT),
            _col("customer_id", ColumnType.BIGINT),
            _col("rating", ColumnType.INTEGER),
            _col("body", ColumnType.TEXT),
        ),
        ("review_id",),
        (Check("rating", "between", 1, 5),),
        delete_pairs=True,
    ),
}
