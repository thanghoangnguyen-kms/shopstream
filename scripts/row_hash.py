"""GATE-05 row-hash helper (RED skeleton: signatures only, implemented in the GREEN commit)."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Protocol


class UnsupportedValueError(TypeError):
    """A cell holds a value this helper refuses to hash."""


class InvalidJSONError(ValueError):
    """A JSON column holds text that is not valid JSON."""


class SupportsToPylist(Protocol):
    def to_pylist(self) -> list[dict[str, object]]: ...


NULL_SENTINEL: bytes = b"\x00"


def canonical_json(value: object) -> str:
    raise NotImplementedError


def encode(value: object, *, json_column: bool, column: str = "") -> bytes:
    raise NotImplementedError


def row_digest(
    row: Mapping[str, object],
    columns: Sequence[str],
    json_columns: Collection[str] = frozenset(),
) -> str:
    raise NotImplementedError


def table_digest(
    rows: Iterable[Mapping[str, object]],
    columns: Sequence[str],
    json_columns: Collection[str] = frozenset(),
) -> tuple[int, str]:
    raise NotImplementedError


def arrow_table_digest(
    table: SupportsToPylist,
    columns: Sequence[str],
    json_columns: Collection[str] = frozenset(),
) -> tuple[int, str]:
    raise NotImplementedError
