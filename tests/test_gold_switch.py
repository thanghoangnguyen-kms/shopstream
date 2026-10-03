"""Unit tests for scripts/gold_switch.py: the publish-id rule, the rename steps, the statistics and the verdict.

Nothing heavy is imported: the rule takes an injected `read` and `sleep`, so these tests need no
DuckDB, no Lakekeeper and no clock. `scripted_read` hands out one id map per read of an object, the
way a poller sees an object change between two reads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import gold_switch as gs
import pytest

OBJECTS = ["fct_orders", "dim_customers", "metricflow_time_spine"]
RETRY_S = 0.25


def uniform(publish_id: str | None) -> dict[str, str | None]:
    return dict.fromkeys(OBJECTS, publish_id)


def scripted_read(
    maps: Sequence[Mapping[str, Any]],
) -> tuple[Callable[[str], Any], dict[str, int]]:
    """A `read` returning maps[n][object] on the n-th read of each object, and the read counts."""
    calls: dict[str, int] = dict.fromkeys(OBJECTS, 0)

    def read(obj: str) -> Any:
        index = calls[obj]
        calls[obj] += 1
        return maps[index][obj]

    return read, calls


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_every_object_at_the_same_id_is_ok() -> None:
    read, _ = scripted_read([uniform("A")])
    kind, ids = gs.read_once(read, OBJECTS)
    assert kind == "ok"
    assert ids == uniform("A")


def test_one_object_at_b_while_the_rest_are_at_a_is_mixed() -> None:
    read, _ = scripted_read([{**uniform("A"), "dim_customers": "B"}])
    kind, ids = gs.read_once(read, OBJECTS)
    assert kind == "mixed"
    assert ids["dim_customers"] == "B"


def test_the_first_object_alone_differing_is_still_mixed() -> None:
    read, _ = scripted_read([{**uniform("B"), "metricflow_time_spine": "A"}])
    assert gs.read_once(read, OBJECTS)[0] == "mixed"


def test_a_missing_object_is_missing_even_when_the_others_agree() -> None:
    read, _ = scripted_read([{**uniform("A"), "fct_orders": None}])
    kind, ids = gs.read_once(read, OBJECTS)
    assert kind == "missing"
    assert ids["fct_orders"] is None


def test_a_missing_object_is_missing_even_when_the_others_differ() -> None:
    read, _ = scripted_read(
        [{"fct_orders": None, "dim_customers": "A", "metricflow_time_spine": "B"}]
    )
    assert gs.read_once(read, OBJECTS)[0] == "missing"


def test_a_read_returning_two_publish_ids_for_one_object_is_a_value_error() -> None:
    read, _ = scripted_read([{**uniform("A"), "fct_orders": ["A", "B"]}])
    with pytest.raises(ValueError, match="fct_orders"):
        gs.read_once(read, OBJECTS)


def test_a_read_returning_a_one_value_list_counts_as_that_value() -> None:
    read, _ = scripted_read([{**uniform("A"), "fct_orders": ["A"]}])
    assert gs.read_once(read, OBJECTS) == ("ok", uniform("A"))


def test_a_read_returning_no_publish_id_at_all_is_a_value_error() -> None:
    read, _ = scripted_read([{**uniform("A"), "fct_orders": []}])
    with pytest.raises(ValueError, match="fct_orders"):
        gs.read_once(read, OBJECTS)


def test_an_ok_first_read_returns_without_sleeping() -> None:
    read, calls = scripted_read([uniform("B")])
    sleep = FakeSleep()
    result = gs.poll(read, OBJECTS, RETRY_S, sleep)
    assert (result.kind, result.retried, result.first_kind) == ("ok", False, "ok")
    assert result.ids == uniform("B")
    assert sleep.calls == []
    assert set(calls.values()) == {1}


def test_a_mixed_then_ok_read_sleeps_once_for_the_retry_delay() -> None:
    read, calls = scripted_read([{**uniform("A"), "dim_customers": "B"}, uniform("B")])
    sleep = FakeSleep()
    result = gs.poll(read, OBJECTS, RETRY_S, sleep)
    assert (result.kind, result.retried, result.first_kind) == ("ok", True, "mixed")
    assert result.ids == uniform("B")
    assert sleep.calls == [RETRY_S]
    assert set(calls.values()) == {2}


def test_a_missing_then_ok_read_is_retried_with_first_kind_missing() -> None:
    read, _ = scripted_read([{**uniform("A"), "fct_orders": None}, uniform("A")])
    sleep = FakeSleep()
    result = gs.poll(read, OBJECTS, RETRY_S, sleep)
    assert (result.kind, result.retried, result.first_kind) == ("ok", True, "missing")
    assert sleep.calls == [RETRY_S]


@pytest.mark.parametrize(
    ("first", "second", "kinds"),
    [
        (
            {**uniform("A"), "dim_customers": "B"},
            {**uniform("A"), "dim_customers": "B"},
            ("mixed", "mixed"),
        ),
        (
            {**uniform("A"), "fct_orders": None},
            {**uniform("A"), "dim_customers": "B"},
            ("missing", "mixed"),
        ),
        (
            {**uniform("A"), "dim_customers": "B"},
            {**uniform("A"), "fct_orders": None},
            ("mixed", "missing"),
        ),
    ],
    ids=["mixed-mixed", "missing-mixed", "mixed-missing"],
)
def test_two_bad_reads_raise_publish_in_progress_and_never_read_a_third_time(
    first: Mapping[str, Any], second: Mapping[str, Any], kinds: tuple[str, str]
) -> None:
    # A third map that would be ok sits in the script: reaching it would hide the failure.
    read, calls = scripted_read([first, second, uniform("A")])
    sleep = FakeSleep()
    with pytest.raises(gs.PublishInProgress) as caught:
        gs.poll(read, OBJECTS, RETRY_S, sleep)
    assert (caught.value.first, caught.value.second) == kinds
    assert set(calls.values()) == {2}
    assert sleep.calls == [RETRY_S]


def test_publish_in_progress_carries_both_kinds() -> None:
    read, _ = scripted_read(
        [{**uniform("A"), "fct_orders": None}, {**uniform("A"), "dim_customers": "B"}]
    )
    with pytest.raises(gs.PublishInProgress) as caught:
        gs.poll(read, OBJECTS, RETRY_S, FakeSleep())
    assert (caught.value.first, caught.value.second) == ("missing", "mixed")
    assert "missing" in str(caught.value)
    assert "mixed" in str(caught.value)


def test_rename_steps_are_three_pairs_in_the_fixed_order() -> None:
    assert gs.rename_steps("fct_orders") == [
        (("gold", "fct_orders"), ("gold_retired", "fct_orders")),
        (("gold_candidate", "fct_orders"), ("gold", "fct_orders")),
        (("gold_retired", "fct_orders"), ("gold_candidate", "fct_orders")),
    ]


def test_two_switches_of_one_object_put_it_back_where_it_started() -> None:
    where = {"gold": "A", "gold_candidate": "B", "gold_retired": None}
    for _ in range(2):
        for (src_ns, name), (dst_ns, dst_name) in gs.rename_steps("fct_orders"):
            assert name == dst_name == "fct_orders"
            assert where[src_ns] is not None
            assert where[dst_ns] is None
            where[dst_ns], where[src_ns] = where[src_ns], None
    assert where == {"gold": "A", "gold_candidate": "B", "gold_retired": None}


def test_the_objects_and_namespaces_are_the_fixed_ones() -> None:
    assert gs.OBJECTS == ("fct_orders", "dim_customers", "metricflow_time_spine")
    assert (gs.GOLD, gs.CANDIDATE, gs.RETIRED) == ("gold", "gold_candidate", "gold_retired")


def test_transitions_counts_changes_between_consecutive_accepted_ids() -> None:
    assert gs.transitions(["A", "A", "B", "B", "A"]) == 2


def test_transitions_reads_a_string_of_ids_and_handles_short_inputs() -> None:
    assert gs.transitions("AABBAB") == 3
    assert gs.transitions("A") == 0
    assert gs.transitions("") == 0


def test_interval_stats_gives_median_and_max_in_milliseconds() -> None:
    stats = gs.interval_stats([10.0, 10.1, 10.2, 10.35], [10.0, 12.0, 30.0])
    poll = stats["poll_interval_ms"]
    switch = stats["switch_ms"]
    assert poll["median"] == pytest.approx(100.0)
    assert poll["max"] == pytest.approx(150.0)
    assert switch["median"] == pytest.approx(12.0)
    assert switch["max"] == pytest.approx(30.0)


def test_interval_stats_with_nothing_to_measure_gives_none() -> None:
    stats = gs.interval_stats([5.0], [])
    assert stats["poll_interval_ms"] == {"median": None, "max": None}
    assert stats["switch_ms"] == {"median": None, "max": None}


def make_view(**overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "view_created": True,
        "view_listed": True,
        "duckdb_reads_view": False,
        "duckdb_error": "CatalogException: Table with name v does not exist!",
    }
    view.update(overrides)
    return view


def make_run(**overrides: Any) -> dict[str, Any]:
    """A natural run that passes every condition: 40 switches, 20 each way, nothing accepted bad."""
    run: dict[str, Any] = {
        "label": "natural",
        "switches": 40,
        "a_to_b": 20,
        "b_to_a": 20,
        "polls": 440,
        "raw_ok": 440,
        "raw_mixed": 0,
        "raw_missing": 0,
        "retried_ok": 0,
        "publish_in_progress": 0,
        "accepted_mixed": 0,
        "accepted_missing": 0,
        "poll_errors": 0,
        "transitions": 40,
        "accepted_ids_seen": ["A", "B"],
        "retry_ms": 250,
        "timing": {
            "poll_interval_ms": {"median": 100.0, "max": 130.0},
            "switch_ms": {"median": 14.0, "max": 27.0},
        },
    }
    run.update(overrides)
    return run


def make_control(**overrides: Any) -> dict[str, Any]:
    """A control run with a 60 ms gap: 10 switches and some raw mixed reads."""
    base: dict[str, Any] = {
        "label": "control",
        "switches": 10,
        "a_to_b": 5,
        "b_to_a": 5,
        "raw_ok": 60,
        "raw_mixed": 7,
        "transitions": 10,
    }
    return make_run(**{**base, **overrides})


NO_OVERRIDE: Any = object()


def verdict(
    view: Mapping[str, Any] | None = None,
    natural: Mapping[str, Any] | None = None,
    control: Any = NO_OVERRIDE,
    *,
    columns_ok: bool = True,
    end_state_ok: bool = True,
) -> dict[str, Any]:
    """item7_verdict over a passing default for whatever the test does not set."""
    return gs.item7_verdict(
        make_view() if view is None else view,
        make_run() if natural is None else natural,
        make_control() if control is NO_OVERRIDE else control,
        columns_ok=columns_ok,
        end_state_ok=end_state_ok,
    )


def test_a_view_duckdb_cannot_read_and_a_clean_natural_run_is_fallback_by_table_rename() -> None:
    result = verdict()
    assert result["verdict"] == "fallback"
    assert result["path"] == "table rename"
    assert result["reasons"]


def test_a_clean_run_with_a_control_that_saw_a_missing_read_is_fallback() -> None:
    result = verdict(control=make_control(raw_mixed=0, raw_missing=1))
    assert (result["verdict"], result["path"]) == ("fallback", "table rename")


def test_a_view_duckdb_can_read_is_inconclusive_because_no_view_repoint_runner_exists() -> None:
    result = verdict(view=make_view(duckdb_reads_view=True))
    assert (result["verdict"], result["path"]) == ("inconclusive", None)
    assert any("view-repoint runner" in reason for reason in result["reasons"])


@pytest.mark.parametrize("missing", ["view_created", "view_listed"])
def test_a_view_that_could_not_be_created_or_listed_is_inconclusive(missing: str) -> None:
    result = verdict(view=make_view(**{missing: False}))
    assert result["verdict"] == "inconclusive"
    assert any("created or listed" in reason for reason in result["reasons"])


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"switches": 38, "a_to_b": 19, "b_to_a": 19, "transitions": 38}, "40 switches"),
        ({"a_to_b": 21, "b_to_a": 19}, "20 each way"),
        ({"accepted_mixed": 1}, "accepted a mixed read"),
        ({"accepted_missing": 1}, "accepted a missing read"),
        ({"poll_errors": 1}, "poll error"),
        ({"transitions": 39}, "transitions"),
        ({"accepted_ids_seen": ["A"]}, "both A and B"),
        ({"retry_ms": 27}, "retry delay"),
    ],
    ids=[
        "too-few-switches",
        "uneven-split",
        "accepted-mixed",
        "accepted-missing",
        "poll-error",
        "transitions-short",
        "only-a-seen",
        "retry-not-above-switch",
    ],
)
def test_each_failed_natural_run_condition_is_inconclusive_and_named(
    overrides: dict[str, Any], needle: str
) -> None:
    result = verdict(natural=make_run(**overrides))
    assert (result["verdict"], result["path"]) == ("inconclusive", None)
    assert any(needle in reason for reason in result["reasons"]), result["reasons"]


def test_publish_in_progress_above_zero_is_inconclusive_and_names_the_retry_delay() -> None:
    result = verdict(natural=make_run(publish_in_progress=2))
    assert result["verdict"] == "inconclusive"
    reason = next(r for r in result["reasons"] if "PublishInProgress" in r)
    assert "250 ms" in reason
    assert "owner decides" in reason


def test_columns_or_end_state_not_ok_is_inconclusive() -> None:
    assert verdict(columns_ok=False)["verdict"] == "inconclusive"
    assert verdict(end_state_ok=False)["verdict"] == "inconclusive"


def test_a_control_run_with_no_raw_mixed_or_missing_read_is_inconclusive() -> None:
    result = verdict(control=make_control(raw_mixed=0, raw_missing=0))
    assert result["verdict"] == "inconclusive"
    assert any("blind" in reason for reason in result["reasons"])


def test_no_control_run_is_inconclusive() -> None:
    result = verdict(control=None)
    assert result["verdict"] == "inconclusive"
    assert any("control run" in reason for reason in result["reasons"])


def test_a_control_run_that_accepted_a_bad_read_is_inconclusive() -> None:
    result = verdict(control=make_control(accepted_mixed=1))
    assert result["verdict"] == "inconclusive"


def test_every_failed_condition_is_listed_not_just_the_first() -> None:
    result = verdict(
        view=make_view(duckdb_reads_view=True),
        natural=make_run(publish_in_progress=1, accepted_mixed=1),
        control=make_control(raw_mixed=0),
    )
    assert len(result["reasons"]) >= 4
