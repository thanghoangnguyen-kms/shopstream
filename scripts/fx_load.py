"""Item 10's throwaway FX loader, run by the `fx-load` one-shot.

ADR-001's Evidence rules exempt a one-off load script for items 10 to 12 from unit tests, so this
file has none; the evidence page records its command line and its seed. The one-shot bind-mounts
this file into the spike image (dlt 1.30.0, DuckDB 1.5.5) on the internal `fx_offline` network,
where the web-only `frankfurter-offline` service answers as http://frankfurter:8080 and nothing
has a route to the internet.

The script first confirms that the seed's backfill is complete (not merely healthy) and shows
that the network is cut, then loads ECB rates through Frankfurter's API in yearly ranges into a
dlt DuckDB destination and prints one JSON line, the last line of stdout. It refuses to load
(exit 1) when the backfill is incomplete or the cut is not shown.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from typing import Any

DEFAULT_URL = "http://frankfurter:8080"
ALLOWED_ORIGINS = frozenset({DEFAULT_URL})
TIMEOUT_S = 120
FIRST_ECB_DATE = dt.date(1999, 1, 4)
# No ECB publication gap since 1999 is longer than 4 days (a Friday to the next Tuesday).
MAX_PUBLICATION_GAP_DAYS = 4
ECB_HOST = "data-api.ecb.europa.eu"
PUBLIC_IP = "1.1.1.1"
CUT_TIMEOUT_S = 5
DB_PATH = "/tmp/fx.duckdb"
PIPELINES_DIR = "/tmp/pl"
DATASET = "bronze"
TABLE = "fx_rates"


def check_url(url: str) -> None:
    """Refuse any URL that is not plain http to an allowed in-network origin."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http":
        raise ValueError("only http URLs are allowed")
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in ALLOWED_ORIGINS:
        raise ValueError(f"origin {origin} is not allowed")


def base_url() -> str:
    url = os.environ.get("FRANKFURTER_URL", DEFAULT_URL).rstrip("/")
    check_url(url)
    return url


def http_get_json(url: str) -> Any:
    """GET one allowed URL and return the decoded JSON body; an HTTP error status raises."""
    check_url(url)
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        return json.loads(response.read())


def first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0]


def ecb_entry(providers: Any) -> Mapping[str, Any] | None:
    """The ECB entry of /v2/providers, whatever key names the provider with its id."""
    if isinstance(providers, Mapping):
        providers = providers.get("providers", list(providers.values()))
    for entry in providers:
        if isinstance(entry, Mapping) and any(
            str(entry.get(key, "")).upper() == "ECB" for key in ("key", "id", "name", "provider")
        ):
            return entry
    return None


def providers_check(today: dt.date | None = None) -> dict[str, Any]:
    """Read /v2/providers and say whether the ECB backfill is complete, not just healthy."""
    today = today or dt.datetime.now(dt.UTC).date()
    entry = ecb_entry(http_get_json(f"{base_url()}/v2/providers"))
    result: dict[str, Any] = {"ecb": dict(entry) if entry is not None else None, "complete": False}
    if entry is None:
        result["reason"] = "no ECB entry in /v2/providers"
        return result
    start = str(entry.get("start_date"))
    end = str(entry.get("end_date"))
    missed = entry.get("publishes_missed")
    reasons = []
    if start != FIRST_ECB_DATE.isoformat():
        reasons.append(f"start_date {start} is not {FIRST_ECB_DATE}")
    if missed != 0:
        reasons.append(f"publishes_missed is {missed}")
    try:
        lag = (today - dt.date.fromisoformat(end)).days
    except ValueError:
        reasons.append(f"end_date {end} is not a date")
    else:
        if lag > MAX_PUBLICATION_GAP_DAYS:
            reasons.append(f"end_date {end} is {lag} days before {today}")
    result["complete"] = not reasons
    result["end_date"] = end
    if reasons:
        result["reason"] = "; ".join(reasons)
    return result


def network_cut() -> dict[str, Any]:
    """Show, not assume, that DNS and a public IP are unreachable from this container."""
    result: dict[str, Any] = {}
    try:
        socket.getaddrinfo(ECB_HOST, 443)
    except OSError as exc:
        result["dns_blocked"] = True
        result["dns_error"] = first_line(exc)
    else:
        result["dns_blocked"] = False
    try:
        socket.create_connection((PUBLIC_IP, 443), timeout=CUT_TIMEOUT_S).close()
    except OSError as exc:
        result["ip_blocked"] = True
        result["ip_error"] = first_line(exc)
    else:
        result["ip_blocked"] = False
    return result


def year_ranges(from_date: dt.date, to_date: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
    for year in range(from_date.year, to_date.year + 1):
        yield max(from_date, dt.date(year, 1, 1)), min(to_date, dt.date(year, 12, 31))


def fetch_range(api: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """One range as (date, base, quote, rate) records, without the carry-in anchor row."""
    base = base_url()
    rows: list[dict[str, Any]]
    if api == "v2":
        body = http_get_json(f"{base}/v2/rates?providers=ECB&from={start}&to={end}")
        rows = [dict(row) for row in body]
    else:
        body = http_get_json(f"{base}/v1/{start}..{end}")
        rows = [
            {"date": day, "base": body["base"], "quote": quote, "rate": rate}
            for day, rates in body["rates"].items()
            for quote, rate in rates.items()
        ]
    # A range whose start is not a publication day also returns the previous publication date.
    return [row for row in rows if start.isoformat() <= str(row["date"]) <= end.isoformat()]


def load_stats(path: str = DB_PATH) -> dict[str, Any]:
    """Counts over the loaded table; the non-EUR rows drop the EUR/EUR identity record."""
    import duckdb

    table = f"{DATASET}.{TABLE}"
    con = duckdb.connect(path, read_only=True)
    try:
        row = con.execute(
            f"SELECT count(*), count(DISTINCT CASE WHEN quote <> 'EUR' THEN date END), "
            f"min(CASE WHEN quote <> 'EUR' THEN CAST(date AS DATE) END), "
            f"max(CASE WHEN quote <> 'EUR' THEN CAST(date AS DATE) END), "
            f"count(*) FILTER (WHERE dayofweek(CAST(date AS DATE)) IN (0, 6)) FROM {table}"
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise RuntimeError("the stats query returned no row")
    return {
        "rows": int(row[0]),
        "distinct_dates": int(row[1]),
        "min_date": str(row[2]),
        "max_date": str(row[3]),
        "weekend_rows": int(row[4]),
    }


def load(api: str, from_date: dt.date, to_date: dt.date) -> dict[str, Any]:
    """Load yearly ranges into the dlt DuckDB destination and return the table's counts."""
    import dlt

    def records() -> Iterator[list[dict[str, Any]]]:
        for start, end in year_ranges(from_date, to_date):
            yield fetch_range(api, start, end)

    resource = dlt.resource(
        records(), name=TABLE, write_disposition="merge", primary_key=["date", "base", "quote"]
    )
    pipeline = dlt.pipeline(
        "fx",
        destination=dlt.destinations.duckdb(DB_PATH),
        dataset_name=DATASET,
        pipelines_dir=PIPELINES_DIR,
    )
    started = time.monotonic()
    pipeline.run(resource)
    seconds = round(time.monotonic() - started, 2)
    return {**load_stats(), "load_seconds": seconds}


def versions() -> dict[str, str]:
    import dlt
    import duckdb

    return {
        "dlt": str(dlt.__version__),
        "duckdb": str(duckdb.__version__),
        "python": sys.version.split()[0],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", default=FIRST_ECB_DATE.isoformat())
    parser.add_argument("--to", dest="to_date", help="default: the ECB end_date")
    parser.add_argument("--api", choices=("v2", "v1"), default="v2")
    args = parser.parse_args(argv)
    report: dict[str, Any] = {"versions": versions(), "api": args.api}
    providers = providers_check()
    cut = network_cut()
    report["providers"] = providers
    report["network_cut"] = cut
    from_date = dt.date.fromisoformat(args.from_date)
    to_date = dt.date.fromisoformat(args.to_date or providers.get("end_date") or args.from_date)
    report["from"] = from_date.isoformat()
    report["to"] = to_date.isoformat()
    refusal = []
    if not providers["complete"]:
        refusal.append(f"the backfill is not complete: {providers.get('reason')}")
    if not (cut["dns_blocked"] and cut["ip_blocked"]):
        refusal.append("the network cut is not shown")
    if refusal:
        report["refused"] = refusal
        print(json.dumps(report, ensure_ascii=False))
        return 1
    report["load"] = load(args.api, from_date, to_date)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
