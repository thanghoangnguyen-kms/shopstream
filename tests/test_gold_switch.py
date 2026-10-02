"""Unit tests for scripts/gold_switch.py: the publish-id rule, the rename steps and (later) the verdict.

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
