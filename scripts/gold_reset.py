"""Skeleton for the gold purge; the RED commit carries signatures only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

ALLOWED_NAMESPACES: frozenset[str] = frozenset({"gold", "gold_candidate", "gold_retired"})


def checked_namespaces(argv: Sequence[str]) -> list[str]:
    raise NotImplementedError


def table_names(listing: Mapping[str, Any], namespace: str) -> list[str]:
    raise NotImplementedError


def reset(namespace: str) -> list[str]:
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError
