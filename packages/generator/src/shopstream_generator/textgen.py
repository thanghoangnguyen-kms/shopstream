"""Word lists and text renderers (stub: the RED commit's placeholder, replaced by GREEN)."""

from __future__ import annotations

WORD_LISTS = ("first_names", "last_names")


def parse_word_list(name: str, data: bytes) -> tuple[str, ...]:
    raise NotImplementedError


def words(name: str) -> tuple[str, ...]:
    raise NotImplementedError


def full_name(first_idx: int, last_idx: int) -> str:
    raise NotImplementedError


def email(first_idx: int, last_idx: int, customer_id: int, version: int) -> str:
    raise NotImplementedError
