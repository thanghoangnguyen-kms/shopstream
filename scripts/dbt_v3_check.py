"""Item 3's check on silver_spike.inc_v3, the table dbt 2.0.6 builds through Lakekeeper.

A spike script run inside the `spark-job` one-shot: `python /app/dbt_v3_check.py reset|inspect`.
`reset` has Lakekeeper purge the table so the next dbt run starts from no table. `inspect` loads
the table from Lakekeeper's REST catalog and prints its format version, table uuid, metadata
location and snapshots sorted by sequence number, plus the rows DuckDB reads, sorted by id. Both
print one compact JSON line, the last line of stdout. The loadTable body, its `config` and its
`storage-credentials` are never printed: Lakekeeper vends keys there even without the delegation
header, so only status codes and the parsed metadata fields leave this script.

This is a spike script (ADR-001 Evidence rules): it needs a live stack and has no unit tests. The
heavy import (duckdb) sits inside `inspect`, so importing this module in CI is safe.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import read_v3
import spark_v3_job

NAMESPACE = "silver_spike"
TABLE = "inc_v3"
# The snapshot summary keys that show what each commit did, in the order they are reported.
SUMMARY_KEYS = (
    "added-data-files",
    "deleted-data-files",
    "added-delete-files",
    "added-position-deletes",
    "added-dvs",
    "added-records",
    "deleted-records",
    "total-records",
)


def table_url() -> str:
    """The loadTable and dropTable URL for the one fixed table."""
    prefix = spark_v3_job.catalog_prefix()
    namespace = read_v3.identifier(NAMESPACE)
    table = read_v3.identifier(TABLE)
    return (
        f"{spark_v3_job.lakekeeper_url()}/catalog/v1/{prefix}/namespaces/{namespace}/tables/{table}"
    )


def reset() -> dict[str, Any]:
    """Have Lakekeeper purge silver_spike.inc_v3; a table that is not there is fine."""
    status, _ = spark_v3_job.http_request("DELETE", f"{table_url()}?purgeRequested=true", {}, None)
    if status not in {204, 404}:
        raise RuntimeError(f"purging {NAMESPACE}.{TABLE} returned HTTP {status}")
    return {"reset": status}


def snapshot_fields(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """One snapshot's sequence number, id, operation and the summary keys that are present."""
    summary: Mapping[str, Any] = snapshot.get("summary", {})
    return {
        "sequence_number": snapshot.get("sequence-number"),
        "snapshot_id": snapshot.get("snapshot-id"),
        "operation": summary.get("operation"),
        "summary": {key: summary[key] for key in SUMMARY_KEYS if key in summary},
    }


def sorted_snapshots(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Snapshots by sequence number: Lakekeeper's listing order is not chronological."""
    fields = [snapshot_fields(snapshot) for snapshot in metadata.get("snapshots", [])]
    return sorted(fields, key=lambda item: int(item["sequence_number"]))


def load_metadata() -> tuple[Mapping[str, Any], str]:
    """The table's `metadata` and `metadata-location`, and nothing else from the response."""
    status, body = spark_v3_job.http_request("GET", table_url(), {}, None)
    if status != 200:
        raise RuntimeError(f"loading {NAMESPACE}.{TABLE} returned HTTP {status}")
    parsed = json.loads(body)
    metadata: Mapping[str, Any] = parsed["metadata"]
    return metadata, str(parsed["metadata-location"])


def read_rows() -> tuple[list[list[Any]], str]:
    """The table's rows through DuckDB, sorted by id, and DuckDB's version."""
    con = read_v3.duckdb_connect()
    con.execute(
        f"ATTACH '{spark_v3_job.WAREHOUSE}' AS lk (TYPE iceberg, "
        f"ENDPOINT '{spark_v3_job.lakekeeper_url()}/catalog', AUTHORIZATION_TYPE 'none')"
    )
    rows = con.execute(
        f"SELECT id, name, is_deleted FROM lk.{NAMESPACE}.{TABLE} ORDER BY id"
    ).fetchall()
    version = str(con.execute("SELECT version()").fetchone()[0])
    return [list(row) for row in rows], version


def inspect() -> dict[str, Any]:
    """The table's catalog metadata and rows; a failed step sets `error` and nulls its fields."""
    result: dict[str, Any] = {
        "table": f"{NAMESPACE}.{TABLE}",
        "catalog_format_version": None,
        "table_uuid": None,
        "metadata_location": None,
        "snapshots": None,
        "rows": None,
        "duckdb_version": None,
        "error": None,
    }
    try:
        metadata, location = load_metadata()
        result["catalog_format_version"] = metadata.get("format-version")
        result["table_uuid"] = metadata.get("table-uuid")
        result["metadata_location"] = location
        result["snapshots"] = sorted_snapshots(metadata)
    except Exception as exc:
        result["error"] = read_v3.error_text(exc)
        return result
    try:
        result["rows"], result["duckdb_version"] = read_rows()
    except Exception as exc:
        result["error"] = read_v3.error_text(exc)
    return result


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1 or argv[0] not in {"reset", "inspect"}:
        print("usage: dbt_v3_check.py reset|inspect", file=sys.stderr)
        return 2
    try:
        result = reset() if argv[0] == "reset" else inspect()
    except Exception as exc:
        result = {"error": read_v3.error_text(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
