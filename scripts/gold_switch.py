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
        super().__init__(f"publish in progress: first read {first}, second read {second}")
        self.first = first
        self.second = second


@dataclass(frozen=True)
class PollResult:
    """What `poll` accepted: the final kind (always ok), its ids, and how it got there."""

    kind: str
    ids: Mapping[str, str | None]
    retried: bool
    first_kind: str


def publish_id_of(obj: str, values: Sequence[str]) -> str:
    """The one publish_id an object holds; ValueError for none or several.

    Two values in one object is a broken or doubled build (an incremental rebuild appends), not a
    switch window, so it is an error and never a `mixed` read.
    """
    distinct = sorted(set(values))
    if len(distinct) != 1:
        raise ValueError(f"{obj} holds {len(distinct)} publish_id values: {distinct}")
    return distinct[0]


def read_once(
    read: Callable[[str], Any], objects: Sequence[str]
) -> tuple[str, dict[str, str | None]]:
    """Read every object once; `read(obj)` returns its publish_id, a list of them, or None.

    None means the object does not exist right now. Returns (kind, ids) with kind `missing` when
    any object is absent, else `mixed` when the ids differ, else `ok`. Every object is read, so a
    round costs exactly one read per object.
    """
    ids: dict[str, str | None] = {}
    for obj in objects:
        value = read(obj)
        if value is None or isinstance(value, str):
            ids[obj] = value
        else:
            ids[obj] = publish_id_of(obj, list(value))
    if None in ids.values():
        return "missing", ids
    return ("ok" if len(set(ids.values())) == 1 else "mixed"), ids


def poll(
    read: Callable[[str], Any],
    objects: Sequence[str],
    retry_delay_s: float,
    sleep: Callable[[float], None],
) -> PollResult:
    """ADR-001's rule: one read, and on a mixed or missing result one retry after the delay.

    A second bad read raises `PublishInProgress`; there is never a third read.
    """
    first_kind, ids = read_once(read, objects)
    if first_kind == "ok":
        return PollResult("ok", ids, False, "ok")
    sleep(retry_delay_s)
    second_kind, ids = read_once(read, objects)
    if second_kind != "ok":
        raise PublishInProgress(first_kind, second_kind)
    return PollResult("ok", ids, True, first_kind)


def rename_steps(obj: str) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """The three REST renames that swap one object between gold and gold_candidate.

    Each pair is ((namespace, name), (namespace, name)): gold to retired, candidate to gold,
    retired to candidate. Between the first and second the object is absent from gold.
    """
    return [
        ((GOLD, obj), (RETIRED, obj)),
        ((CANDIDATE, obj), (GOLD, obj)),
        ((RETIRED, obj), (CANDIDATE, obj)),
    ]


def transitions(ids: Sequence[str]) -> int:
    raise NotImplementedError


def interval_stats(
    poll_starts_s: Sequence[float], switch_ms: Sequence[float]
) -> dict[str, dict[str, float | None]]:
    raise NotImplementedError


def item7_verdict(
    view_check: Mapping[str, Any],
    natural: Mapping[str, Any],
    control: Mapping[str, Any] | None,
    expected_switches: int = 40,
    *,
    columns_ok: bool,
    end_state_ok: bool,
) -> dict[str, Any]:
    raise NotImplementedError
