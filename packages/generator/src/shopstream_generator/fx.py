"""Fail-closed readers for the vendored ECB snapshot and the latest quotes (FX-01, P8's domain).

Both files are package data under `shopstream_generator/data/`, written once by
`scripts/ecb_vendor.py build` and read here through `importlib.resources`, so nothing touches
the network or a path outside the package. Each file records its own SHA-256; every load
recomputes it and raises `SnapshotError`, naming the file and the check, when anything
disagrees. Rates are `Decimal`s built from the published text, never a float.

The engine calls only `latest_quotes` (D-11: every non-EUR line is priced from one fixed
latest-quote factor, so no rate is keyed by date). `snapshot` serves the tests and later
evidence.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from importlib.resources import files

DATA_FILES = ("ecb_rates.csv", "ecb_latest.json")
RATES_FILE, LATEST_FILE = DATA_FILES
EXPECTED_LATEST = 30
HEADER_KEYS = ("source", "fetched", "rows", "max_move_bp", "sha256")
_RATE = re.compile(r"[0-9]+(\.[0-9]+)?")
_CODE = re.compile(r"[A-Z]{3}")


class SnapshotError(ValueError):
    """A vendored data file is missing, altered or not in the shape this reader accepts."""


@dataclass(frozen=True)
class Snapshot:
    """The wide snapshot: one `Decimal` (or `None` where the currency isn't quoted) per cell."""

    meta: dict[str, str]
    dates: tuple[date, ...]
    currencies: tuple[str, ...]
    cells: dict[str, tuple[Decimal | None, ...]]


def _fail(name: str, check: str) -> SnapshotError:
    return SnapshotError(f"{name}: {check}")


def _read(name: str) -> bytes:
    """The bytes of one package-data file; only the names in DATA_FILES are ever asked for."""
    if name not in DATA_FILES:
        raise SnapshotError(f"{name}: not a vendored data file")
    try:
        return files("shopstream_generator").joinpath("data", name).read_bytes()
    except OSError as exc:
        raise _fail(name, f"cannot be read ({type(exc).__name__})") from exc


def _rate(name: str, text: str) -> Decimal:
    if not _RATE.fullmatch(text):
        raise _fail(name, f"rate text {text!r} is not plain digits with an optional fraction")
    return Decimal(text)


def _header(name: str, data: bytes) -> tuple[dict[str, str], bytes]:
    """The `# key: value` comment block and the bytes after it."""
    meta: dict[str, str] = {}
    offset = 0
    while data.startswith(b"# ", offset):
        end = data.find(b"\n", offset)
        if end < 0:
            raise _fail(name, "a header line has no line ending")
        key, sep, value = data[offset + 2 : end].decode("utf-8", "replace").partition(": ")
        if not sep or key in meta:
            raise _fail(name, f"header line {key!r} is malformed or repeated")
        meta[key] = value
        offset = end + 1
    missing = [key for key in HEADER_KEYS if key not in meta]
    if missing:
        raise _fail(name, f"header lacks {', '.join(missing)}")
    return meta, data[offset:]


def parse_snapshot(data: bytes) -> Snapshot:
    """Verify the body checksum against the header, then read every cell as a Decimal or None."""
    name = RATES_FILE
    meta, body = _header(name, data)
    if hashlib.sha256(body).hexdigest() != meta["sha256"]:
        raise _fail(name, "checksum does not match the header's sha256")
    try:
        text = body.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise _fail(name, f"body is not readable CSV ({type(exc).__name__})") from exc
    if not rows or rows[0][:1] != ["date"]:
        raise _fail(name, "first column is not 'date'")
    codes = tuple(rows[0][1:])
    if list(codes) != sorted(set(codes)) or not all(_CODE.fullmatch(code) for code in codes):
        raise _fail(name, "currency columns are not sorted, unique three-letter codes")
    dates: list[date] = []
    columns: dict[str, list[Decimal | None]] = {code: [] for code in codes}
    for row in rows[1:]:
        if len(row) != len(rows[0]):
            raise _fail(name, f"row {row[:1]} does not have {len(rows[0])} cells")
        try:
            day = date.fromisoformat(row[0])
        except ValueError as exc:
            raise _fail(name, f"{row[0]!r} is not an ISO date") from exc
        if dates and day <= dates[-1]:
            raise _fail(name, f"{day} does not ascend strictly")
        dates.append(day)
        for code, cell in zip(codes, row[1:], strict=True):
            columns[code].append(_rate(name, cell) if cell else None)
    return Snapshot(
        meta=meta,
        dates=tuple(dates),
        currencies=codes,
        cells={code: tuple(values) for code, values in columns.items()},
    )


def parse_latest(data: bytes) -> dict[str, Decimal]:
    """Verify the quotes' canonical-JSON checksum, then return 30 Decimals in code order."""
    name = LATEST_FILE
    try:
        document = json.loads(data, parse_float=str, parse_int=str)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail(name, f"is not JSON ({type(exc).__name__})") from exc
    meta = document.get("meta") if isinstance(document, dict) else None
    quotes = document.get("quotes") if isinstance(document, dict) else None
    if not isinstance(meta, dict) or not isinstance(quotes, dict):
        raise _fail(name, "lacks the meta and quotes objects")
    if not all(isinstance(code, str) and isinstance(text, str) for code, text in quotes.items()):
        raise _fail(name, "a quote is not a string")
    canonical = json.dumps(quotes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if hashlib.sha256(canonical.encode()).hexdigest() != meta.get("sha256"):
        raise _fail(name, "checksum does not match meta.sha256")
    if len(quotes) != EXPECTED_LATEST or "EUR" not in quotes or "BGN" in quotes:
        raise _fail(name, f"is not {EXPECTED_LATEST} codes including EUR and excluding BGN")
    if not all(_CODE.fullmatch(code) for code in quotes):
        raise _fail(name, "a currency code is not three capital letters")
    result = {code: _rate(name, quotes[code]) for code in sorted(quotes)}
    if result["EUR"] != 1:
        raise _fail(name, "EUR is not 1")
    return result


@cache
def _latest() -> tuple[tuple[str, Decimal], ...]:
    return tuple(parse_latest(_read(LATEST_FILE)).items())


def latest_quotes() -> dict[str, Decimal]:
    """The latest publication's quotes, 29 currencies plus EUR, verified once per process."""
    return dict(_latest())


@cache
def snapshot() -> Snapshot:
    """The whole vendored window, verified once per process."""
    return parse_snapshot(_read(RATES_FILE))
