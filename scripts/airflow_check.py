"""Item 6's measurement (signatures-only stubs for the RED run)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TOPIC = "fx.refresh"
DAG_ID = "fx_refresh_on_message"
LIMIT_S = 60
RUNS_SQL = ""
EVENTS_SQL = ""


def parse_iso_ms(text: str | None) -> int:
    raise NotImplementedError


def item6_verdict(
    messages: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    limit_s: float = LIMIT_S,
) -> dict[str, Any]:
    raise NotImplementedError


def psql_argv(sql: str) -> list[str]:
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError
