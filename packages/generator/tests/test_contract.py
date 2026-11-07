"""The test-only contract reader returns the contract's facts and fails closed on anything else."""

from __future__ import annotations

from pathlib import Path

import pytest

from . import contract

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "contracts" / "clickstream" / "page_view.odcs.yaml"
# The tuple literal is the only thing that catches a dropped or renamed enum value.
PAGE_TYPES = ("home", "category", "search", "product", "cart", "checkout")
REQUIRED = frozenset({"event_id", "event_ts", "visitor_id", "page_type"})


def test_the_contract_yields_its_required_fields_and_page_types() -> None:
    parsed = contract.load(CONTRACT)
    assert parsed.required == REQUIRED
    assert parsed.page_types == PAGE_TYPES


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("apiVersion: v3.2.0", "apiVersion: v3.1.0"),
        (
            "        required: true\n        primaryKey",
            "        requierd: true\n        primaryKey",
        ),
        ("        enum:\n", "        enumm:\n"),
        ("          - value: home\n", "          - value: home\n            label: Home\n"),
        ("          - value: home\n", "          - value: category\n"),
        ("name: event_ts", "name: event_at"),
    ],
    ids=["api-version", "typo-required", "typo-enum", "enum-entry-key", "enum-repeat", "renamed"],
)
def test_the_reader_fails_closed(old: str, new: str) -> None:
    source = CONTRACT.read_text(encoding="utf-8")
    assert old in source
    with pytest.raises(contract.ContractError):
        contract.parse(source.replace(old, new, 1))


@pytest.mark.parametrize(
    "text",
    [
        "apiVersion: v3.1.0\n",
        "[]\n",
        "apiVersion: v3.2.0\nschema: []\n",
    ],
    ids=["wrong-api-version-only", "not-a-mapping", "empty-schema"],
)
def test_the_reader_rejects_documents_that_are_not_the_contract(text: str) -> None:
    with pytest.raises(contract.ContractError):
        contract.parse(text)
