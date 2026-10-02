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

import statistics
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
    """How many times consecutive accepted publish ids differ (A, A, B, B, A gives 2)."""
    return sum(1 for index in range(1, len(ids)) if ids[index] != ids[index - 1])


def _median_max(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "max": None}
    return {"median": statistics.median(values), "max": max(values)}


def interval_stats(
    poll_starts_s: Sequence[float], switch_ms: Sequence[float]
) -> dict[str, dict[str, float | None]]:
    """Median and max, in milliseconds, of the gaps between poll starts and of the switch times.

    `poll_starts_s` are time.monotonic() readings in seconds; `switch_ms` are already in
    milliseconds. With nothing to measure the figures are None, never 0.
    """
    gaps_ms = [
        (poll_starts_s[index] - poll_starts_s[index - 1]) * 1000
        for index in range(1, len(poll_starts_s))
    ]
    return {"poll_interval_ms": _median_max(gaps_ms), "switch_ms": _median_max(switch_ms)}


def _run_reasons(run: Mapping[str, Any], expected_switches: int | None, label: str) -> list[str]:
    """The failed conditions shared by the natural and control runs, each named."""
    failed: list[str] = []
    if expected_switches is not None:
        if run["switches"] != expected_switches:
            failed.append(
                f"the {label} run made {run['switches']} switches, expected "
                f"{expected_switches} switches"
            )
        half = expected_switches // 2
        if (run["a_to_b"], run["b_to_a"]) != (half, half):
            failed.append(
                f"the {label} run split {run['a_to_b']} A to B and {run['b_to_a']} B to A, "
                f"expected {half} each way"
            )
    if run["accepted_mixed"]:
        failed.append(f"the {label} run accepted a mixed read {run['accepted_mixed']} times")
    if run["accepted_missing"]:
        failed.append(f"the {label} run accepted a missing read {run['accepted_missing']} times")
    if run["poll_errors"]:
        failed.append(f"the {label} run had {run['poll_errors']} poll error(s)")
    if run["transitions"] != run["switches"]:
        failed.append(
            f"the {label} run observed {run['transitions']} transitions for "
            f"{run['switches']} switches"
        )
    return failed


def item7_verdict(
    view_check: Mapping[str, Any],
    natural: Mapping[str, Any],
    control: Mapping[str, Any] | None,
    expected_switches: int = 40,
    *,
    columns_ok: bool,
    end_state_ok: bool,
) -> dict[str, Any]:
    """Item 7's verdict from the view check, the natural run and the widened-window control run.

    `fallback` (path `table rename`) when DuckDB cannot read the view Lakekeeper lists and every
    natural-run and control-run condition holds. `inconclusive` otherwise, with every failed
    condition named: DuckDB being able to read the view is inconclusive too, because the go path
    (view repoint) needs a runner this module's callers do not have, so `go` is never returned
    here. A natural run with PublishInProgress above 0 is inconclusive: ADR-001 fixes no further
    fallback, so the owner decides.
    """
    failed: list[str] = []
    if not (view_check["view_created"] and view_check["view_listed"]):
        failed.append(
            "the view could not be created or listed through Lakekeeper's REST API "
            f"(create HTTP {view_check.get('create_status')}, "
            f"list HTTP {view_check.get('list_status')})"
        )
    elif view_check["duckdb_reads_view"]:
        failed.append(
            "DuckDB can read the Iceberg view, so the go path needs a view-repoint runner that "
            "has not been built; the owner decides"
        )

    natural_failed = _run_reasons(natural, expected_switches, "natural")
    failed.extend(natural_failed)
    retry_ms = natural["retry_ms"]
    if natural["publish_in_progress"]:
        failed.append(
            f"PublishInProgress was {natural['publish_in_progress']} in the natural run with a "
            f"retry delay of {retry_ms} ms; ADR-001 fixes no further fallback, so the owner decides"
        )
    if set(natural["accepted_ids_seen"]) != {"A", "B"}:
        failed.append(
            f"the poller accepted {sorted(natural['accepted_ids_seen'])}, expected both A and B"
        )
    longest = natural["timing"]["switch_ms"]["max"]
    if longest is None or not retry_ms > longest:
        failed.append(
            f"the retry delay ({retry_ms} ms) is not above the longest natural switch ({longest} ms)"
        )
    if not columns_ok:
        failed.append("a gold object lacks the publish_id column")
    if not end_state_ok:
        failed.append("the end state is not gold A, gold_candidate B and gold_retired empty")

    if control is None:
        failed.append("there is no control run, so the poller was never shown to see the window")
    else:
        control_failed = _run_reasons(control, None, "control")
        failed.extend(control_failed)
        if control["raw_mixed"] + control["raw_missing"] < 1:
            failed.append(
                "the control run counted no raw mixed or missing read, so the poller would be "
                "blind to the switch window"
            )

    if failed:
        return {"verdict": "inconclusive", "path": None, "reasons": failed}
    facts = [
        "DuckDB cannot read the Iceberg view that Lakekeeper lists, so view repoint is unavailable",
        f"the natural run made {natural['switches']} switches, {natural['a_to_b']} A to B and "
        f"{natural['b_to_a']} B to A, and accepted no mixed or missing read",
        f"PublishInProgress was 0 with a retry delay of {retry_ms} ms above the longest "
        f"natural switch ({longest} ms)",
    ]
    if control is not None:
        facts.append(
            f"the control run counted {control['raw_mixed']} raw mixed and "
            f"{control['raw_missing']} raw missing reads, so the poller sees the window"
        )
    return {"verdict": "fallback", "path": "table rename", "reasons": facts}
