"""Simulated time as integer microseconds since the Unix epoch (ADR-004 C1, C2).

Everything inside the generator keeps time as an `int` of microseconds. A timestamp only
becomes text at the edge, through `render`, and only becomes an object through `to_datetime`.
Both go through `EPOCH + timedelta(microseconds=n)`: no float and no platform conversion
(`fromtimestamp`, the local time zone) ever sees a value, so the result is the same on every
machine.

The tick rule lives here too: `next_tick` is the one place that says a tick is never earlier
than its due time and always later than the tick before it (D-01, C2).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

US_PER_SECOND = 1_000_000
US_PER_MINUTE = 60 * US_PER_SECOND
US_PER_HOUR = 60 * US_PER_MINUTE
US_PER_DAY = 24 * US_PER_HOUR

_MICROSECOND = timedelta(microseconds=1)
_RENDERED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")


def to_datetime(us: int) -> datetime:
    """The aware UTC datetime for `us` microseconds since the epoch."""
    return EPOCH + timedelta(microseconds=us)


def render(us: int) -> str:
    """`YYYY-MM-DDTHH:MM:SS.ffffffZ`: always six fraction digits, always the `Z` spelling of UTC."""
    text = to_datetime(us).isoformat(timespec="microseconds")
    return text.removesuffix("+00:00") + "Z"


def parse(text: str) -> int:
    """The inverse of `render`; any other spelling of a time raises ValueError."""
    if _RENDERED.fullmatch(text) is None:
        raise ValueError("time must be YYYY-MM-DDTHH:MM:SS.ffffffZ")
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("time must be a real YYYY-MM-DDTHH:MM:SS.ffffffZ instant") from None
    return (moment - EPOCH) // _MICROSECOND


def next_tick(due_us: int, last_us: int) -> int:
    """The tick for an item due at `due_us` when the previous tick was `last_us` (D-01, C2).

    Two items due at the same microsecond become two ticks: the later one is shifted to one
    microsecond after the earlier, so ticks rise strictly.
    """
    return max(due_us, last_us + 1)
