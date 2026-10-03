"""Unit tests for scripts/gold_reset.py: the namespace allowlist and the list-tables parser.

No network: the listings are hand-built bodies in the shape Lakekeeper's list-tables returns.
"""

from __future__ import annotations

from typing import Any

import gold_reset as gr
import pytest


def listing(*identifiers: tuple[list[str], str]) -> dict[str, Any]:
    return {
        "identifiers": [{"namespace": ns, "name": name} for ns, name in identifiers],
        "next-page-token": None,
    }


def test_the_allowlist_is_exactly_the_three_gold_namespaces() -> None:
    assert sorted(gr.ALLOWED_NAMESPACES) == ["gold", "gold_candidate", "gold_retired"]


@pytest.mark.parametrize(
    "argv",
    [["gold"], ["gold_candidate"], ["gold_retired"], ["gold", "gold_candidate", "gold_retired"]],
)
def test_any_duplicate_free_list_of_gold_namespaces_is_accepted_in_order(argv: list[str]) -> None:
    assert gr.checked_namespaces(argv) == argv


def test_an_empty_list_is_refused() -> None:
    with pytest.raises(ValueError, match="namespace"):
        gr.checked_namespaces([])


def test_a_duplicate_namespace_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        gr.checked_namespaces(["gold", "gold"])


@pytest.mark.parametrize(
    "name", ["silver_spike", "spike_v3", "", "Gold", " gold", "gold.x", "gold/../x"]
)
def test_every_other_namespace_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="not a gold namespace"):
        gr.checked_namespaces(["gold", name])


def test_one_refused_name_stops_the_whole_list() -> None:
    with pytest.raises(ValueError, match="not a gold namespace"):
        gr.checked_namespaces(["silver_spike", "gold"])


def test_table_names_are_sorted_and_only_from_the_asked_namespace() -> None:
    body = listing(
        (["gold"], "metricflow_time_spine"),
        (["gold_candidate"], "fct_orders"),
        (["gold"], "fct_orders"),
        (["gold", "nested"], "deep"),
    )
    assert gr.table_names(body, "gold") == ["fct_orders", "metricflow_time_spine"]


def test_an_empty_identifiers_list_has_no_tables() -> None:
    assert gr.table_names(listing(), "gold") == []


def test_a_namespace_with_no_matching_identifier_has_no_tables() -> None:
    assert gr.table_names(listing((["gold_candidate"], "fct_orders")), "gold") == []


def test_a_table_name_that_is_not_a_plain_identifier_is_refused() -> None:
    with pytest.raises(ValueError, match="identifier"):
        gr.table_names(listing((["gold"], "fct_orders?x=1")), "gold")
