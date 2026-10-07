"""RED stub: the names and signatures of the vendoring helper, with no behavior yet."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

FIRST = date(2024, 12, 1)
CUT = date(2026, 10, 2)
EXPECTED_LATEST = 30
MAX_MOVE_BP = Decimal(1000)
SOURCE = ""


class VendorError(ValueError):
    pass


@dataclass(frozen=True)
class Vendored:
    rates_csv: bytes
    latest_json: bytes
    stats: dict[str, object]


def parse_rates(body: bytes) -> list[dict[str, str]]:
    return []


def max_move(
    dates: Sequence[str], columns: Sequence[str], cells: Mapping[str, Mapping[str, str]]
) -> tuple[Decimal, str, str]:
    return Decimal(0), "", ""


def build(rows: Sequence[Mapping[str, str]], fetched: date) -> Vendored:
    return Vendored(b"", b"", {})


def fetch() -> list[dict[str, str]]:
    return []


def main(argv: Sequence[str] | None = None) -> int:
    return 0
