"""Item 5's bronze reader: find a Postgres row in Lakekeeper's bronze tables on the audit branch.

Run inside the `spark-job` one-shot, which shares the Compose network with Lakekeeper:
`python /app/cdc_check.py bronze-find --table customers --pk 900001 --timeout 240`. The last line of
stdout is one compact JSON line. Plan 04-01's tracer uses `ensure-namespace`, `refs` and `bronze-find`;
Plans 04-02 and 04-03 extend this module with the exactly-once and rollback checks and their pure,
unit-tested rules.

This is a spike script (ADR-001 Evidence rules): the live readers need a running stack and have no
unit tests. The heavy import (pyiceberg) sits inside `bronze_catalog`, so importing this module in CI is
safe. Every REST call goes through the origin-allowlisted `spark_v3_job.http_request`, every table name
through `read_v3.identifier`, and no vended credential is ever printed.

The bronze tables hold the raw Debezium envelope (`before`, `after`, `source`, `op`, ...) plus the
KafkaMetadataTransform columns, so a row's primary key comes from `after`, or from `before` for a delete.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from read_v3 import error_text, identifier
from spark_v3_job import WAREHOUSE, catalog_prefix, http_request, lakekeeper_url

NAMESPACE = "bronze"
PK_COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": ("customer_id",),
    "products": ("product_id",),
    "orders": ("order_id",),
    "order_items": ("order_id", "line_no"),
    "reviews": ("review_id",),
}
KAFKA_META_COLUMNS = (
    "_kafka_metadata_topic",
    "_kafka_metadata_partition",
    "_kafka_metadata_offset",
    "_kafka_metadata_timestamp",
)
BOOTSTRAP_DEFAULT = "kafka-1:19092,kafka-2:19092,kafka-3:19092"
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", BOOTSTRAP_DEFAULT)
AUDIT_BRANCH = "audit"
POLL_INTERVAL_S = 5.0
JSON_HEADERS = {"Content-Type": "application/json"}
ENVELOPE_COLUMNS = ("before", "after", "source", "op", *KAFKA_META_COLUMNS)


def bronze_catalog() -> Any:
    """The Lakekeeper REST catalog through PyIceberg, with vended credentials (never printed)."""
    from pyiceberg.catalog import load_catalog

    return load_catalog(
        "rest",
        uri=f"{lakekeeper_url()}/catalog",
        warehouse=WAREHOUSE,
        **{"header.X-Iceberg-Access-Delegation": "vended-credentials"},
    )


def ensure_namespace() -> None:
    """Create the bronze namespace through the REST API; 200 or 409 (it exists) are both fine."""
    body = json.dumps({"namespace": [identifier(NAMESPACE)], "properties": {}}).encode()
    url = f"{lakekeeper_url()}/catalog/v1/{catalog_prefix()}/namespaces"
    status, payload = http_request("POST", url, JSON_HEADERS, body)
    if status not in {200, 409}:
        raise RuntimeError(
            f"creating namespace {NAMESPACE} returned HTTP {status}: "
            f"{payload.decode(errors='replace')[:200]}"
        )


def load_bronze(table: str) -> Any:
    return bronze_catalog().load_table(f"{identifier(NAMESPACE)}.{identifier(table)}")


def refs(table: str) -> dict[str, int]:
    """The table's branch and tag names with their snapshot ids; a fresh sink table has no main."""
    return {name: int(ref.snapshot_id) for name, ref in load_bronze(table).refs().items()}


def key_of(row: Mapping[str, Any], table: str) -> tuple[str, ...] | None:
    """The row's primary key as strings, from `after`, else `before`; None when both are null."""
    image = row.get("after") or row.get("before")
    if not isinstance(image, Mapping):
        return None
    return tuple(str(image[column]) for column in PK_COLUMNS[table])


def parse_pk(table: str, text: str) -> tuple[str, ...]:
    """`--pk` as a tuple: one value, or comma-separated values in PK_COLUMNS order."""
    parts = tuple(part.strip() for part in text.split(","))
    if len(parts) != len(PK_COLUMNS[table]) or not all(parts):
        raise ValueError(f"--pk needs {len(PK_COLUMNS[table])} value(s) for {table}")
    return parts


def has_struct(iceberg_table: Any, column: str) -> bool:
    field = next((f for f in iceberg_table.schema().fields if f.name == column), None)
    return field is not None and "struct" in str(field.field_type).lower()


def bronze_find(table: str, pk: tuple[str, ...], timeout_s: float) -> dict[str, Any]:
    """Poll the audit branch every 5 s until a row with primary key `pk` lands; return its facts.

    The sink creates the table on its first commit, so a missing table is polled, not an error.
    When several rows match (an insert then updates) the one with the greatest source.lsn wins.
    `has_before_after` says the table holds both envelope images as structs (a create's `before` is
    null by design). Raises TimeoutError when nothing lands in time.
    """
    if table not in PK_COLUMNS:
        raise ValueError(f"unknown table {table}")
    from pyiceberg.exceptions import NoSuchTableError

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            iceberg_table = load_bronze(table)
        except NoSuchTableError:
            # The sink creates the table on its first commit, so the table may not exist yet.
            iceberg_table = None
        found = iceberg_table.refs() if iceberg_table is not None else {}
        if iceberg_table is not None and AUDIT_BRANCH in found:
            scan = iceberg_table.scan(
                snapshot_id=int(found[AUDIT_BRANCH].snapshot_id), selected_fields=ENVELOPE_COLUMNS
            )
            matches = [
                row
                for row in scan.to_arrow().to_pylist()
                if key_of(row, table) == pk and row.get("source") is not None
            ]
            if matches:
                best = max(matches, key=lambda row: int(row["source"]["lsn"]))
                return {
                    "table": table,
                    "refs": {name: int(ref.snapshot_id) for name, ref in found.items()},
                    "op": best["op"],
                    "lsn": int(best["source"]["lsn"]),
                    "partition": int(best["_kafka_metadata_partition"]),
                    "offset": int(best["_kafka_metadata_offset"]),
                    "has_before_after": has_struct(iceberg_table, "before")
                    and has_struct(iceberg_table, "after"),
                    "before_is_null": best["before"] is None,
                    "matches": len(matches),
                }
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"no bronze.{table} row with key {','.join(pk)} on {AUDIT_BRANCH} "
                f"after {timeout_s:g} s (refs: {sorted(found)})"
            )
        time.sleep(POLL_INTERVAL_S)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read Debezium envelopes from bronze.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ensure-namespace", help="create the bronze namespace if absent")
    refs_cmd = commands.add_parser("refs", help="print a bronze table's refs")
    refs_cmd.add_argument("--table", required=True, choices=sorted(PK_COLUMNS))
    find = commands.add_parser("bronze-find", help="wait for one row on the audit branch")
    find.add_argument("--table", required=True, choices=sorted(PK_COLUMNS))
    find.add_argument(
        "--pk", required=True, help="primary key value (comma-separated if composite)"
    )
    find.add_argument("--timeout", type=float, default=240.0, help="seconds to wait")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "ensure-namespace":
            ensure_namespace()
            print(json.dumps({"namespace": NAMESPACE, "ensured": True}), flush=True)
        elif args.command == "refs":
            print(json.dumps({"table": args.table, "refs": refs(args.table)}), flush=True)
        else:
            result = bronze_find(args.table, parse_pk(args.table, args.pk), args.timeout)
            print(json.dumps(result, sort_keys=True), flush=True)
    except Exception as exc:
        print(f"error: {error_text(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
