"""The vocabulary the engine speaks: tables, operations and ticks (D-01, D-19).

An `Op` is one row operation on one table. A `Tick` is one engine step, which is one Postgres
transaction later: a strictly rising timestamp, a sequence number and at least one op. The
seven stream names (six tables plus the clickstream `page_view`) have one fixed order, which
the manifest and every report follow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Table(StrEnum):
    """The six captured tables, named as in REF section 7."""

    CUSTOMERS = "customers"
    PRODUCTS = "products"
    ORDERS = "orders"
    ORDER_ITEMS = "order_items"
    PAYMENTS = "payments"
    REVIEWS = "reviews"


PAGE_VIEW = "page_view"

STREAMS: tuple[str, ...] = (*(table.value for table in Table), PAGE_VIEW)


class OpKind(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class Op:
    """One row operation; `row` is the full after-image, and is None exactly for a delete."""

    table: Table
    kind: OpKind
    key: Mapping[str, int]
    row: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if (self.row is None) != (self.kind is OpKind.DELETE):
            raise ValueError("an op has a row exactly when it is not a delete")


@dataclass(frozen=True)
class Tick:
    """One engine step: its sequence number, its timestamp in microseconds and its ops."""

    seq: int
    ts_us: int
    ops: tuple[Op, ...]
