"""RED stub for the test-only clickstream contract reader: it validates nothing yet."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ContractError(ValueError):
    """The contract is not the one this reader understands."""


@dataclass(frozen=True)
class Contract:
    required: frozenset[str]
    page_types: tuple[str, ...]


def parse(text: str) -> Contract:
    return Contract(required=frozenset(), page_types=())


def load(path: Path) -> Contract:
    return parse(path.read_text(encoding="utf-8"))
