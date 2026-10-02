"""Skeleton for item 4's MetricFlow comparison; the RED commit carries signatures only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

HAND_SQL = ""
HAND_TOTAL_SQL = ""
DBT_CLI = "/opt/dbt/bin/dbt"
MF_DBT_CLI = "/opt/mf/bin/dbt"
MF_CLI = "/opt/mf/bin/mf"


@dataclass(frozen=True)
class Comparison:
    match: bool
    empty: bool
    missing_days: tuple[str | None, ...]
    extra_days: tuple[str | None, ...]
    differing: tuple[tuple[str | None, Decimal, Decimal], ...]
    duplicate_days: tuple[str | None, ...]


def parse_mf_csv(text: str) -> list[tuple[str | None, str]]:
    raise NotImplementedError


def normalise_rows(rows: Sequence[tuple[object, object]]) -> list[tuple[str | None, Decimal]]:
    raise NotImplementedError


def compare_rows(
    mf_rows: Sequence[tuple[str | None, Decimal]], sql_rows: Sequence[tuple[str | None, Decimal]]
) -> Comparison:
    raise NotImplementedError


def dbt_core_version(version_output: str) -> str | None:
    raise NotImplementedError


def item4_verdict(
    *,
    validate_exit: int,
    query_exits: Sequence[int],
    comparisons: Sequence[Comparison],
    cli_versions: Mapping[str, str | None],
) -> tuple[str, list[str]]:
    raise NotImplementedError


def main() -> int:
    raise NotImplementedError
