"""D-10: money is integer cents in state, a Decimal at exponent -2 only at the row boundary.

Every rounding goes through `money.MONEY`, a local ROUND_HALF_UP context, never the global
context: a half-cent rounds up here where Python's default context (half-even) would round
0.125 down.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from shopstream_generator import money
from shopstream_generator.money import CENT, MONEY


def test_the_local_context_rounds_a_half_cent_up_where_the_default_rounds_it_down() -> None:
    half = Decimal("0.125")
    assert MONEY.quantize(half, CENT) == Decimal("0.13")
    assert half.quantize(CENT) == Decimal("0.12")


@pytest.mark.parametrize(
    ("cents", "text"),
    [(0, "0.00"), (1, "0.01"), (1234, "12.34"), (50000, "500.00"), (10**12, "10000000000.00")],
)
def test_cents_to_decimal_is_exact_at_exponent_minus_two(cents: int, text: str) -> None:
    value = money.cents_to_decimal(cents)
    assert value == Decimal(text)
    assert value.as_tuple().exponent == -2
    assert format(value, "f") == text


@given(st.integers(min_value=0, max_value=10**15), st.integers(min_value=1, max_value=10**9))
def test_half_up_div_equals_the_money_context_division(numerator: int, denominator: int) -> None:
    expected = MONEY.quantize(MONEY.divide(Decimal(numerator), Decimal(denominator)), Decimal(1))
    assert money.half_up_div(numerator, denominator) == int(expected)


def test_half_up_div_rounds_a_tie_up() -> None:
    assert money.half_up_div(5, 10) == 1
    assert money.half_up_div(4, 10) == 0
    assert money.half_up_div(15, 10) == 2
    assert money.half_up_div(0, 7) == 0


@pytest.mark.parametrize(("numerator", "denominator"), [(-1, 3), (1, -3), (1, 0), (-1, 0)])
def test_half_up_div_refuses_a_negative_argument_or_a_zero_denominator(
    numerator: int, denominator: int
) -> None:
    with pytest.raises(ValueError, match="half_up_div"):
        money.half_up_div(numerator, denominator)
