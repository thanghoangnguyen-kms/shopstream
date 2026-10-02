"""Purge every table of the named gold namespaces through Lakekeeper's REST purge.

Run inside the `spark-job` one-shot: `python /app/gold_reset.py <namespace> [...]`. Gold is built
`incremental` into an empty namespace (a second build appends, and DuckDB's Iceberg cannot rename or
replace a table inside the creating transaction), so every rebuild starts from purged objects. The
namespace names are checked against ALLOWED_NAMESPACES before any request is sent: anything outside
gold, gold_candidate and gold_retired is refused, so the purge can never touch silver_spike,
spike_v3 or any other namespace. Names pass `read_v3.identifier`, and every call goes through the
origin-allowlisted `spark_v3_job.http_request`.

This is a spike script (ADR-001 Evidence rules): `reset` needs a live Lakekeeper and has no unit
test; the allowlist and the listing parser do. It prints one compact JSON line, the last line of
stdout, and exits 0 when it purged (or found nothing), 1 on an error and 2 on a refused or empty
argument list.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

import spark_v3_job
from read_v3 import identifier

ALLOWED_NAMESPACES: frozenset[str] = frozenset({"gold", "gold_candidate", "gold_retired"})


def checked_namespaces(argv: Sequence[str]) -> list[str]:
    """The namespaces to purge, in the order given; ValueError for anything not purgeable."""
    if not argv:
        raise ValueError("name at least one namespace to purge")
    if len(set(argv)) != len(argv):
        raise ValueError("a namespace is named twice (duplicate)")
    for name in argv:
        if name not in ALLOWED_NAMESPACES:
            raise ValueError(f"{name!r} is not a gold namespace")
    return list(argv)


def table_names(listing: Mapping[str, Any], namespace: str) -> list[str]:
    """The sorted table names whose namespace is exactly `namespace` in a list-tables body."""
    names: list[str] = []
    for item in listing.get("identifiers") or []:
        if list(item.get("namespace", [])) == [namespace]:
            names.append(identifier(str(item["name"])))
    return sorted(names)


def tables_url(namespace: str) -> str:
    prefix = spark_v3_job.catalog_prefix()
    return f"{spark_v3_job.lakekeeper_url()}/catalog/v1/{prefix}/namespaces/{identifier(namespace)}/tables"


def list_tables(namespace: str) -> list[str]:
    """Every table in the namespace, following `next-page-token` until it is absent."""
    base = tables_url(namespace)
    names: list[str] = []
    page = ""
    while True:
        url = f"{base}?pageToken={urllib.parse.quote(page)}" if page else base
        status, body = spark_v3_job.http_request("GET", url, {}, None)
        if status == 404:
            return names
        if status != 200:
            raise RuntimeError(f"listing {namespace} returned HTTP {status}")
        listing = json.loads(body)
        names.extend(table_names(listing, namespace))
        page = str(listing.get("next-page-token") or "")
        if not page:
            return sorted(names)


def reset(namespace: str) -> list[str]:
    """Purge every table of one gold namespace; return the names purged (a 404 counts as gone)."""
    base = tables_url(namespace)
    names = list_tables(namespace)
    for name in names:
        status, _ = spark_v3_job.http_request(
            "DELETE", f"{base}/{name}?purgeRequested=true", {}, None
        )
        if status not in {204, 404}:
            raise RuntimeError(f"purging {namespace}.{name} returned HTTP {status}")
    return names


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        namespaces = checked_namespaces(args)
    except ValueError as exc:
        print(json.dumps({"error": str(exc), "allowed": sorted(ALLOWED_NAMESPACES)}))
        return 2
    purged: dict[str, list[str]] = {}
    try:
        for namespace in namespaces:
            purged[namespace] = reset(namespace)
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:200]}", "purged": purged}))
        return 1
    print(json.dumps({"purged": purged}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
