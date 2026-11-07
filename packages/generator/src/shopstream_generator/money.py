"""Money: integer cents in state, a Decimal at exponent -2 only at the row boundary (D-10).

Every amount the engine remembers is an integer number of cents (euros for a list price, a line
total in the order's currency later). A `Decimal` is built from it only when a row is rendered,
and every rounding goes through `MONEY`, a local context with ROUND_HALF_UP and the default 28
digits. Nothing in this package reads or sets the global decimal context, so another module,
a test or a dependency changing it can't change an amount.

`half_up_div` is the integer form of the same rounding, for a conversion that must stay in
integers: it divides two non-negative ints and rounds a half up. `convert_cents` is the one
fixed-factor currency conversion, through the local context.
"""

from __future__ import annotations

import decimal
from decimal import ROUND_HALF_UP, Decimal

MONEY = decimal.Context(prec=28, rounding=ROUND_HALF_UP)
CENT = Decimal("0.01")


def cents_to_decimal(cents: int) -> Decimal:
    """`cents` as a Decimal at exponent -2: 1234 is 12.34 and 0 is 0.00."""
    return MONEY.scaleb(Decimal(cents), -2)


def half_up_div(numerator: int, denominator: int) -> int:
    """`numerator / denominator` rounded to an int, a half going up; both must be sane."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("half_up_div needs numerator >= 0 and denominator > 0")
    return (2 * numerator + denominator) // (2 * denominator)


def convert_cents(cents: int, factor: Decimal) -> int:
    """`cents` times `factor`, rounded half up to a whole cent: a fixed-factor conversion (D-11).

    It goes through `MONEY` only, so it is the same on every machine. The quantized value has
    exponent -2 by construction, so scaling it by 100 is an exact integer.
    """
    if cents < 0:
        raise ValueError("convert_cents needs cents >= 0")
    if not factor.is_finite() or factor <= 0:
        raise ValueError("convert_cents needs a finite factor above 0")
    quantized = MONEY.quantize(MONEY.multiply(cents_to_decimal(cents), factor), CENT)
    return int(MONEY.scaleb(quantized, 2))
