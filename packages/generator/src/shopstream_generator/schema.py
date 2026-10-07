"""The six captured tables as data: stub, filled in by the next commit."""

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


SCHEMAS: Mapping[Table, TableSchema] = {}
