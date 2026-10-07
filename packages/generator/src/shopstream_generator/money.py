"""The money context and integer-cent helpers (stub: the RED commit's placeholder)."""

from __future__ import annotations

import decimal
from decimal import Decimal

MONEY = decimal.Context(prec=28)
CENT = Decimal("0.01")


def cents_to_decimal(cents: int) -> Decimal:
    raise NotImplementedError


def half_up_div(numerator: int, denominator: int) -> int:
    raise NotImplementedError
