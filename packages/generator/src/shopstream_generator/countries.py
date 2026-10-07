"""The country table reader (stub: the RED commit's placeholder, replaced by the GREEN commit)."""

from __future__ import annotations

from dataclasses import dataclass


class ReferenceDataError(ValueError):
    """A committed reference file is missing or not in the shape the generator accepts."""


@dataclass(frozen=True)
class Country:
    code: str
    currency: str
    weight: int
    cities: tuple[str, ...]


def parse_countries(text: str) -> tuple[Country, ...]:
    raise NotImplementedError


def countries() -> tuple[Country, ...]:
    raise NotImplementedError


def country_weights() -> tuple[int, ...]:
    raise NotImplementedError
