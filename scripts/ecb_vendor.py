"""One-off vendoring helper for the generator's ECB snapshot.

`fetch` runs inside the `fx-load` container against the read-only `frankfurter-offline` service
and prints the raw rows, one canonical JSON object per line. `build` runs on the host, is pure
(rows in, bytes out), and is the only writer of `ecb_rates.csv` and `ecb_latest.json` under the
generator's package data. Both files record their SHA-256 and the measured largest consecutive
move, and `build` refuses any input outside the expected shape or beyond the 10 % bound: it never
loosens the bound, edits a rate or drops a currency to make a snapshot fit (ADR-005, P7).

Rates never pass through a float: the response is parsed with the number types set to `str`, and
a rate must be plain digits with an optional fraction. The module is stdlib-only plus `fx_load`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Context, Decimal
from pathlib import Path

import fx_load

FIRST = date(2024, 12, 1)
CUT = date(2026, 10, 2)
EXPECTED_LATEST = 30
MAX_MOVE_BP = Decimal(1000)
SOURCE = "Frankfurter 2.5.1 GET /v2/rates?providers=ECB (ECB euro foreign exchange reference rates)"

RATE_TEXT = re.compile(r"[0-9]+(\.[0-9]+)?")
FIELDS = ("date", "base", "quote", "rate")
BP_STEP = Decimal("0.01")
BP_PER_UNIT = Decimal(10000)
RETIRED = "BGN"
BASE = "EUR"


class VendorError(ValueError):
    """The input is outside the shape or bounds the snapshot is allowed to have."""


@dataclass(frozen=True)
class Vendored:
    rates_csv: bytes
    latest_json: bytes
    stats: dict[str, object]


def parse_rates(body: bytes) -> list[dict[str, str]]:
    """Rows of a v2 range response, every field as the text the service sent."""
    try:
        parsed = json.loads(body, parse_float=str, parse_int=str)
    except ValueError as exc:
        raise VendorError(f"the body is not JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise VendorError("the body is not a JSON array of rows")
    rows: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise VendorError(f"a row is not a JSON object: {item!r}")
        row: dict[str, str] = {}
        for field in FIELDS:
            value = item.get(field)
            if not isinstance(value, str):
                raise VendorError(f"a row has no text {field}: {item!r}")
            row[field] = value
        if RATE_TEXT.fullmatch(row["rate"]) is None:
            raise VendorError(f"rate {row['rate']!r} is not plain digits: {item!r}")
        rows.append(row)
    return rows


def max_move(
    dates: Sequence[str], columns: Sequence[str], cells: Mapping[str, Mapping[str, str]]
) -> tuple[Decimal, str, str]:
    """The largest consecutive move in basis points, with its currency and the later date.

    `cells[date][code]` is the rate text. A currency is compared only across publications where it
    is quoted on both, so an unquoted day is a break, never a zero. The move is computed in
    Decimal under a local context and recorded to 0.01 bp; ties keep the first currency and date.
    """
    ctx = Context(prec=28)
    best: tuple[Decimal, str, str] | None = None
    for code in columns:
        previous: Decimal | None = None
        for day in dates:
            text = cells.get(day, {}).get(code)
            if text is None:
                previous = None
                continue
            rate = Decimal(text)
            if previous is not None:
                change = ctx.multiply(ctx.abs(ctx.subtract(rate, previous)), BP_PER_UNIT)
                move = ctx.divide(change, previous).quantize(
                    BP_STEP, rounding=ROUND_HALF_UP, context=ctx
                )
                if best is None or move > best[0]:
                    best = (move, code, day)
            previous = rate
    return best if best is not None else (Decimal("0.00"), "", "")


def day_of(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise VendorError(f"{text!r} is not an ISO date") from exc


def collect(rows: Sequence[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Validated rows as cells[date][code], dropping every row dated after the cut first."""
    cells: dict[str, dict[str, str]] = {}
    for row in rows:
        when = day_of(row["date"])
        if when > CUT:
            continue
        if when < FIRST:
            raise VendorError(f"{row['date']} is before the window start {FIRST}")
        if row["base"] != BASE:
            raise VendorError(f"base {row['base']!r} on {row['date']} is not {BASE}")
        text = row["rate"]
        if RATE_TEXT.fullmatch(text) is None:
            raise VendorError(f"rate {text!r} for {row['quote']} on {row['date']} is not digits")
        if Decimal(text) <= 0:
            raise VendorError(f"rate {text} for {row['quote']} on {row['date']} is not positive")
        if row["quote"] == BASE and Decimal(text) != 1:
            raise VendorError(f"{BASE} is {text} on {row['date']}, not 1")
        day_cells = cells.setdefault(row["date"], {})
        seen = day_cells.setdefault(row["quote"], text)
        if seen != text:
            raise VendorError(
                f"conflicting rates for {row['quote']} on {row['date']}: {seen} and {text}"
            )
    if not cells:
        raise VendorError(f"no rows on or before {CUT}")
    return cells


def check_latest(latest: Mapping[str, str], day: str) -> None:
    if len(latest) != EXPECTED_LATEST:
        raise VendorError(
            f"the latest publication {day} holds {len(latest)} quotes, expected {EXPECTED_LATEST}"
        )
    if BASE not in latest:
        raise VendorError(f"the latest publication {day} has no {BASE} quote")
    if RETIRED in latest:
        raise VendorError(f"the latest publication {day} still quotes retired {RETIRED}")


def build(rows: Sequence[Mapping[str, str]], fetched: date) -> Vendored:
    """Both data files from raw rows, byte-identical for any order of the same rows."""
    cells = collect(rows)
    dates = sorted(cells)
    columns = sorted({code for day_cells in cells.values() for code in day_cells})
    latest_day = dates[-1]
    latest = {code: cells[latest_day][code] for code in sorted(cells[latest_day])}
    check_latest(latest, latest_day)
    move, move_code, move_day = max_move(dates, columns, cells)
    if move > MAX_MOVE_BP:
        raise VendorError(
            f"the largest consecutive move is {move} bp ({move_code} on {move_day}), "
            f"above the {MAX_MOVE_BP} bp bound; the bound is not loosened here"
        )
    quoted = sum(len(day_cells) for day_cells in cells.values())

    lines = ["date," + ",".join(columns)]
    lines += [day + "," + ",".join(cells[day].get(code, "") for code in columns) for day in dates]
    body = ("\n".join(lines) + "\n").encode()
    where = f"{move_code} on {move_day}" if move_code else "none"
    header = [
        f"# source: {SOURCE}",
        f"# fetched: {fetched.isoformat()}; cut: {CUT.isoformat()}",
        f"# rows: {len(dates)} publications x {len(columns)} currencies; {quoted} quoted cells",
        f"# max_move_bp: {move} ({where})",
        f"# sha256: {hashlib.sha256(body).hexdigest()}",
    ]
    rates_csv = ("\n".join(header) + "\n").encode() + body

    canonical = json.dumps(latest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    document = {
        "meta": {
            "count": len(latest),
            "cut": CUT.isoformat(),
            "date": latest_day,
            "fetched": fetched.isoformat(),
            "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "source": SOURCE,
        },
        "quotes": latest,
    }
    latest_json = (
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode()

    stats: dict[str, object] = {
        "publications": len(dates),
        "columns": len(columns),
        "cells": quoted,
        "latest_date": latest_day,
        "latest_count": len(latest),
        "max_move_bp": str(move),
        "max_move_currency": move_code,
        "max_move_date": move_day,
    }
    return Vendored(rates_csv, latest_json, stats)


def fetch() -> list[dict[str, str]]:
    """Container side: the ECB rows of the window, read through the offline service."""
    check = fx_load.providers_check(today=CUT)
    if not check["complete"]:
        raise VendorError(f"the Frankfurter backfill is not complete: {check.get('reason')}")
    base = fx_load.base_url()
    rows: list[dict[str, str]] = []
    for start, end in fx_load.year_ranges(FIRST, CUT):
        url = f"{base}/v2/rates?providers=ECB&from={start}&to={end}"
        fx_load.check_url(url)
        with urllib.request.urlopen(url, timeout=fx_load.TIMEOUT_S) as response:
            body = response.read()
        # A range starting on a non-publication day also returns the previous publication.
        rows += [
            row for row in parse_rates(body) if start.isoformat() <= row["date"] <= end.isoformat()
        ]
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="print the raw rows as NDJSON (inside the fx-load container)")
    build_parser = commands.add_parser("build", help="write both data files from the raw rows")
    build_parser.add_argument("--raw", type=Path, required=True, help="the NDJSON fetch printed")
    build_parser.add_argument("--out-dir", type=Path, required=True, help="the package data dir")
    build_parser.add_argument("--fetched", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            for row in fetch():
                print(json.dumps(row, sort_keys=True, separators=(",", ":")))
            return 0
        lines = [line for line in args.raw.read_text().splitlines() if line.strip()]
        built = build(parse_rates(("[" + ",".join(lines) + "]").encode()), args.fetched)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "ecb_rates.csv").write_bytes(built.rates_csv)
        (args.out_dir / "ecb_latest.json").write_bytes(built.latest_json)
        print(json.dumps(built.stats, sort_keys=True))
        return 0
    except (VendorError, OSError) as exc:
        print(f"ecb_vendor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
