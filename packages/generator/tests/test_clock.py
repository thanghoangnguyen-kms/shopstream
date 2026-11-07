"""The simulated clock: integer microseconds, one rendering, one tick rule (HASH-01, CORE-02)."""

from __future__ import annotations

import pytest
from shopstream_generator import clock

GOLDEN_START = "2025-06-28T00:00:00.000000Z"
GOLDEN_END = "2025-07-05T00:00:00.000000Z"
DRIFT_INSTANT = "2025-07-01T00:00:00.000000Z"


def test_render_zero_is_the_epoch_with_six_digits() -> None:
    assert clock.render(0) == "1970-01-01T00:00:00.000000Z"


def test_a_microsecond_keeps_its_leading_zeros() -> None:
    assert clock.render(5) == "1970-01-01T00:00:00.000005Z"


@pytest.mark.parametrize("text", [GOLDEN_START, GOLDEN_END, DRIFT_INSTANT])
def test_parse_inverts_render(text: str) -> None:
    assert clock.render(clock.parse(text)) == text


def test_the_golden_window_holds_the_drift_instant() -> None:
    assert clock.parse(GOLDEN_START) == 1751068800000000
    assert clock.parse(DRIFT_INSTANT) == 1751328000000000
    assert clock.parse(GOLDEN_START) <= clock.parse(DRIFT_INSTANT) < clock.parse(GOLDEN_END)
    assert clock.parse(GOLDEN_END) - clock.parse(GOLDEN_START) == 7 * clock.US_PER_DAY


@pytest.mark.parametrize(
    "text",
    [
        "2025-06-28T00:00Z",
        "2025-06-28T00:00:00.000000+00:00",
        "2025-06-28T00:00:00.000000",
        "2025-06-28T00:00:00Z",
        "2025-06-28 00:00:00.000000Z",
        "2025-02-30T00:00:00.000000Z",
        "",
    ],
    ids=["no-seconds", "plus-suffix", "no-z", "no-fraction", "space", "bad-date", "empty"],
)
def test_parse_rejects_every_other_spelling(text: str) -> None:
    with pytest.raises(ValueError, match="time"):
        clock.parse(text)


def test_the_unit_constants_are_integers_and_agree() -> None:
    assert clock.US_PER_SECOND == 1_000_000
    assert clock.US_PER_MINUTE == 60_000_000
    assert clock.US_PER_HOUR == 3_600_000_000
    assert clock.US_PER_DAY == 86_400_000_000
    assert all(
        type(v) is int
        for v in (clock.US_PER_SECOND, clock.US_PER_MINUTE, clock.US_PER_HOUR, clock.US_PER_DAY)
    )


def test_next_tick_shifts_a_tie_and_keeps_a_later_due_time() -> None:
    assert clock.next_tick(5, 5) == 6
    assert clock.next_tick(9, 5) == 9
    assert clock.next_tick(3, 5) == 6


def test_to_datetime_is_aware_utc() -> None:
    moment = clock.to_datetime(clock.parse(DRIFT_INSTANT))
    assert moment.utcoffset() is not None
    assert moment.isoformat() == "2025-07-01T00:00:00+00:00"
