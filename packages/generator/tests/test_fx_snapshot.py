"""P7 on the vendored ECB snapshot, the latest-quote domain (P8) and FX-01, offline.

Every test reads the committed package data through `shopstream_generator.fx` and recomputes
what the data's headers claim: both SHA-256 values, the largest consecutive move, the 13,695
quoted cells W2's item 10 loaded, and the TARGET coverage of the window. The module patches
socket connections to raise, so a test that tried the network would fail instead of passing
on a live service (FX-01, T-02-08).

"No gap exceeds 4 days" is read as ADR-004's `days_carried <= 4`: consecutive publications are
at most 5 calendar days apart, and the measured 5-day gaps are Easter and Christmas 2025 and
Easter 2026. Recording that reading in the specs is Phase 4's EVID-02, not this plan.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal
from itertools import pairwise
from typing import NoReturn

import pytest
from shopstream_generator import fx

from . import target_calendar

FIRST_WINDOW = date(2024, 12, 1)
CUT = date(2026, 10, 2)
BACKFILL_START = date(2025, 1, 1)
MAX_MOVE_BP = Decimal(1000)
W2_CELLS = 13_695
PUBLICATIONS = 468
LATEST_CODES = 30
MAX_CARRIED_DAYS = 4
SIZE_CAP = 500 * 1024
MOVE_CONTEXT = Context(prec=28)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: socket.socket, address: object) -> NoReturn:
        raise OSError(f"test_fx_snapshot.py refuses a network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_the_network_guard_refuses_a_connection() -> None:
    with socket.socket() as sock, pytest.raises(OSError, match="refuses a network connection"):
        sock.connect(("127.0.0.1", 9))


# the calendar


@pytest.mark.parametrize(
    ("year", "easter"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5))],
)
def test_easter_sunday(year: int, easter: date) -> None:
    assert target_calendar.easter_sunday(year) == easter


def test_target_day_counts() -> None:
    assert len(target_calendar.target_days(date(2025, 1, 1), date(2025, 12, 31))) == 255
    assert len(target_calendar.target_days(FIRST_WINDOW, CUT)) == PUBLICATIONS


def test_target_closing_days() -> None:
    closed = [
        date(2025, 1, 1),
        date(2025, 4, 18),  # Good Friday
        date(2025, 4, 21),  # Easter Monday
        date(2025, 5, 1),
        date(2025, 12, 25),
        date(2025, 12, 26),
        date(2025, 12, 27),  # a Saturday
    ]
    assert not any(target_calendar.is_target_business_day(day) for day in closed)
    assert target_calendar.is_target_business_day(date(2025, 12, 24))


# the checksums


def _bump_first_digit(data: bytes, pattern: bytes) -> bytes:
    """`data` with the first digit after `pattern`'s match changed: a one-digit rate edit."""
    match = re.search(pattern, data)
    assert match is not None
    at = match.end()
    assert data[at : at + 1].isdigit()
    return data[:at] + str((int(data[at : at + 1]) + 1) % 10).encode() + data[at + 1 :]


FIRST_CSV_RATE = rb"\n\d{4}-\d{2}-\d{2},"
FIRST_JSON_QUOTE = rb'"quotes": \{\s*"[A-Z]{3}": "'


def test_the_vendored_files_load_and_verify() -> None:
    assert len(fx.snapshot().dates) == PUBLICATIONS
    assert len(fx.latest_quotes()) == LATEST_CODES


def test_a_one_digit_edit_of_a_rate_fails_the_snapshot_checksum() -> None:
    data = fx._read(fx.RATES_FILE)
    assert fx.parse_snapshot(data).dates  # the untouched bytes pass the same function
    with pytest.raises(fx.SnapshotError, match=r"ecb_rates\.csv: checksum"):
        fx.parse_snapshot(_bump_first_digit(data, FIRST_CSV_RATE))


def test_a_one_digit_edit_of_a_quote_fails_the_latest_checksum() -> None:
    data = fx._read(fx.LATEST_FILE)
    assert fx.parse_latest(data)  # the untouched bytes pass the same function
    with pytest.raises(fx.SnapshotError, match=r"ecb_latest\.json: checksum"):
        fx.parse_latest(_bump_first_digit(data, FIRST_JSON_QUOTE))


def test_a_header_that_lost_its_checksum_is_refused() -> None:
    data = fx._read(fx.RATES_FILE)
    stripped = b"\n".join(line for line in data.split(b"\n") if not line.startswith(b"# sha256"))
    with pytest.raises(fx.SnapshotError, match="header lacks sha256"):
        fx.parse_snapshot(stripped)


def test_only_the_vendored_names_can_be_read() -> None:
    with pytest.raises(fx.SnapshotError, match="not a vendored data file"):
        fx._read("../fx.py")


def test_latest_quotes_hands_out_a_copy() -> None:
    quotes = fx.latest_quotes()
    quotes.clear()
    assert len(fx.latest_quotes()) == LATEST_CODES


# P7


def largest_moves(snap: fx.Snapshot) -> tuple[Decimal, str, date]:
    """The largest consecutive move in bp, to 0.01 bp, with its currency and later date.

    Computed in Decimal with a local context. A move counts only where the currency is quoted
    on both of two adjacent publications; ties keep the first currency and date.
    """
    best: tuple[Decimal, str, date] = (Decimal(0), "", snap.dates[0])
    for code in snap.currencies:
        series = snap.cells[code]
        for index in range(1, len(series)):
            before, after = series[index - 1], series[index]
            if before is None or after is None:
                continue
            move = MOVE_CONTEXT.divide(MOVE_CONTEXT.multiply(abs(after - before), 10000), before)
            move = move.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if move > best[0]:
                best = (move, code, snap.dates[index])
    return best


def carried_days(dates: Sequence[date]) -> int:
    """The most calendar days a consecutive pair of publications leaves carried (ADR-004)."""
    return max((later - earlier).days - 1 for earlier, later in pairwise(dates))


def test_the_first_publication_leads_the_default_backfill_start_by_four_days() -> None:
    first = fx.snapshot().dates[0]
    assert first <= BACKFILL_START - timedelta(days=MAX_CARRIED_DAYS)  # 2024-12-28


def test_every_rate_is_above_zero() -> None:
    snap = fx.snapshot()
    rates = [cell for series in snap.cells.values() for cell in series if cell is not None]
    assert rates
    assert all(rate > 0 for rate in rates)


def test_no_consecutive_move_exceeds_ten_percent_and_the_largest_is_the_recorded_one() -> None:
    snap = fx.snapshot()
    move, code, day = largest_moves(snap)
    assert move <= MAX_MOVE_BP
    recorded = snap.meta["max_move_bp"]
    assert recorded == f"{move} ({code} on {day})"


def test_publication_dates_are_exactly_the_target_days_of_the_window() -> None:
    assert list(fx.snapshot().dates) == target_calendar.target_days(FIRST_WINDOW, CUT)


def has_hole(series: Sequence[Decimal | None]) -> bool:
    """True when an empty cell sits between a currency's first and last quoted publication."""
    quoted = [index for index, cell in enumerate(series) if cell is not None]
    return bool(quoted) and None in series[quoted[0] : quoted[-1] + 1]


def test_every_currency_has_a_value_on_every_day_inside_its_quoted_span() -> None:
    snap = fx.snapshot()
    for code, series in snap.cells.items():
        assert any(cell is not None for cell in series), code
        assert not has_hole(series), code


def test_eur_is_one_on_every_publication() -> None:
    assert set(fx.snapshot().cells["EUR"]) == {Decimal(1)}


def test_no_gap_carries_more_than_four_days() -> None:
    """ADR-004's days_carried <= 4: 5 calendar days apart carries 4; Easter and Christmas do."""
    dates = fx.snapshot().dates
    assert carried_days(dates) == MAX_CARRIED_DAYS
    five_day = [later for earlier, later in pairwise(dates) if (later - earlier).days == 5]
    assert five_day == [date(2025, 4, 22), date(2025, 12, 29), date(2026, 4, 7)]


def test_the_gap_check_has_teeth_at_four_and_five_carried_days() -> None:
    start = date(2025, 3, 3)
    assert carried_days([start, start + timedelta(days=5)]) == 4
    assert carried_days([start, start + timedelta(days=6)]) == 5


def test_an_empty_cell_is_not_quoted_never_zero_and_a_mid_span_one_fails() -> None:
    one = Decimal(1)
    assert has_hole((one, None, one))
    assert not has_hole((None, None, one, one))  # not yet quoted
    assert not has_hole((one, one, None, None))  # no longer quoted, as BGN after 2025-12-31
    assert fx.snapshot().cells["BGN"][-1] is None


# W2 and P8's domain


def test_the_quoted_cells_from_2025_match_the_w2_row_count() -> None:
    snap = fx.snapshot()
    since = [index for index, day in enumerate(snap.dates) if day >= BACKFILL_START]
    quoted = sum(
        1 for series in snap.cells.values() for index in since if series[index] is not None
    )
    assert quoted == W2_CELLS


def test_the_latest_quotes_are_29_currencies_plus_eur_without_bgn() -> None:
    latest = fx.latest_quotes()
    assert len(latest) == LATEST_CODES
    assert "EUR" in latest
    assert "BGN" not in latest
    assert latest["EUR"] == 1
    assert list(latest) == sorted(latest)


def test_the_latest_quotes_are_the_last_publication_of_the_snapshot() -> None:
    snap = fx.snapshot()
    latest = fx.latest_quotes()
    last = {code: series[-1] for code, series in snap.cells.items() if series[-1] is not None}
    assert last == latest


def test_bgn_was_last_quoted_on_2025_12_31() -> None:
    snap = fx.snapshot()
    quoted = [
        day for day, cell in zip(snap.dates, snap.cells["BGN"], strict=True) if cell is not None
    ]
    assert quoted[-1] == date(2025, 12, 31)


# size


@pytest.mark.parametrize("name", fx.DATA_FILES)
def test_each_data_file_is_under_the_large_file_cap(name: str) -> None:
    assert len(fx._read(name)) < SIZE_CAP
