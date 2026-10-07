"""The test-side TARGET calendar (D-28): the days the ECB publishes euro reference rates.

TARGET2 is closed on weekends and on 1 January, Good Friday, Easter Monday, 1 May, 25 December
and 26 December (docs/specs/transform/ref-bus-matrix.md section 5). The generator never needs
this, so it lives with the tests: P7 uses it to prove the snapshot holds a publication on every
such day of its window and on no other. Easter comes from the anonymous Gregorian algorithm,
which needs only integer `//` and `%`.
"""

from __future__ import annotations

from datetime import date, timedelta


def easter_sunday(year: int) -> date:
    """The Gregorian Easter Sunday of `year` (Meeus/Jones/Butcher, integer arithmetic only)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def is_target_business_day(day: date) -> bool:
    """True on Monday to Friday except the six TARGET closing days."""
    if day.weekday() >= 5:
        return False
    easter = easter_sunday(day.year)
    closed = {
        date(day.year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(day.year, 5, 1),
        date(day.year, 12, 25),
        date(day.year, 12, 26),
    }
    return day not in closed


def target_days(first: date, last: date) -> list[date]:
    """Every TARGET business day from `first` to `last`, both inclusive."""
    days = (last - first).days
    return [
        day
        for day in (first + timedelta(days=offset) for offset in range(days + 1))
        if is_target_business_day(day)
    ]
