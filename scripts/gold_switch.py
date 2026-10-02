"""ADR-001's publish-id rule as reader-side code, plus the gold switch's rename steps.

Each gold object carries the publish_id of the build that wrote it. A reader that sees two values
across the objects of one query has caught a switch half-way (`mixed`), and a reader that finds an
object absent has caught the instant between two renames (`missing`). Either way it retries once
after a delay; a second bad read raises `PublishInProgress`, which callers count and never hide.
Week 21 reuses this module in the semantic layer and the MCP server.

The module is pure and stdlib only: the live reads, the renames and the threads are in
`gold_switch_probe.py`, so the rule is tested with injected readers and a fake sleep.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

OBJECTS: tuple[str, ...] = ("fct_orders", "dim_customers", "metricflow_time_spine")
GOLD = "gold"
CANDIDATE = "gold_candidate"
RETIRED = "gold_retired"


class PublishInProgress(RuntimeError):
    """A second read in a row was mixed or missing; carries the kind of both reads."""

    def __init__(self, first: str, second: str) -> None:
        raise NotImplementedError

    first: str
    second: str


@dataclass(frozen=True)
class PollResult:
    kind: str
    ids: Mapping[str, str | None]
    retried: bool
    first_kind: str


def read_once(
    read: Callable[[str], Any], objects: Sequence[str]
) -> tuple[str, dict[str, str | None]]:
    raise NotImplementedError


def poll(
    read: Callable[[str], Any],
    objects: Sequence[str],
    retry_delay_s: float,
    sleep: Callable[[float], None],
) -> PollResult:
    raise NotImplementedError


def rename_steps(obj: str) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    raise NotImplementedError
