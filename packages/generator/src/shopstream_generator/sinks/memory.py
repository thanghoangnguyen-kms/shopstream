"""The Postgres-like oracle sink: stub, filled in by the next commit."""

from __future__ import annotations

from collections.abc import Mapping

from ..ops import Table, Tick


class CdcViolation(ValueError):
    """A tick the sink refuses; names the table, the check and its kind, never a row value."""

    def __init__(self, table: Table | None, check: str, kind: str, column: str | None = None):
        self.table = table
        self.check = check
        self.kind = kind
        self.column = column
        where = "tick" if table is None else table.value
        suffix = "" if column is None else f" (column {column})"
        super().__init__(f"{where}: {kind} check {check} failed{suffix}")


class MemoryCdcSink:
    def __init__(self) -> None:
        self.last_seq = 0
        self.last_ts_us: int | None = None

    def commit(self, tick: Tick) -> None:
        raise NotImplementedError

    def table(self, table: Table) -> Mapping[tuple[int, ...], Mapping[str, object]]:
        raise NotImplementedError
