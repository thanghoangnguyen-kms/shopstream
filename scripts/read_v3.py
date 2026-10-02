"""Item 2's readers: DuckDB, PyIceberg and Polars read Spark's four v3 tables and are compared.

Run inside the `spark-job` one-shot right after `spark_v3_job.py --out /tmp/spark.json`:
`python /app/read_v3.py /tmp/spark.json`. The file holds the writer's JSON line. Every reader
reads each table at the snapshot id Spark reported, hashes its Arrow rows through
`row_hash.arrow_table_digest` with the job's column list, and the script prints one compact JSON
line, the last line of stdout: `versions`, the 4 tables x 4 readers `matrix` (Spark is the
reference) and the item 2 verdict. A reader that cannot load a table is a `cannot-load` cell with
the error type and first line, never zero rows and never dropped. The vended credentials PyIceberg
loads and Polars needs as storage options live only in memory and are never printed.

This is a spike script (ADR-001 Evidence rules): the reader functions need a live stack and have
no unit tests; the pure helpers and the verdict rule do. The heavy imports (duckdb, pyiceberg,
polars) sit inside the reader functions, so importing this module in CI is safe.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import row_hash
from spark_v3_job import COLUMNS, DEFAULT_LAKEKEEPER_URL, JSON_COLUMNS, WAREHOUSE

READERS = ("spark", "duckdb", "pyiceberg", "polars")
DUCKDB_EXTENSIONS = "/opt/duckdb/extensions"
ERROR_LIMIT = 200
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def error_text(exc: BaseException) -> str:
    """The exception's type name, a colon and the first line of its message, at most 200 chars."""
    lines = str(exc).splitlines()
    first = lines[0] if lines else ""
    return f"{type(exc).__name__}: {first}"[:ERROR_LIMIT]


def cell_result(spark_table: Mapping[str, Any], row_count: int, digest: str) -> str:
    """`match` only when the row count and the table digest both equal Spark's.

    The snapshot id is pinned by construction (every reader reads Spark's id), so it is not
    compared here. Equal counts with a different digest is a mismatch.
    """
    same = row_count == spark_table["row_count"] and digest == spark_table["table_digest"]
    return "match" if same else "mismatch"


def matrix_row(
    table: str,
    reader: str,
    result: str,
    snapshot_id: int | None,
    row_count: int | None,
    table_digest: str | None,
    current_snapshot_id: int | None,
    error: str | None,
) -> dict[str, Any]:
    """One cell of the parity matrix, with every field present."""
    return {
        "table": table,
        "reader": reader,
        "result": result,
        "snapshot_id": snapshot_id,
        "row_count": row_count,
        "table_digest": table_digest,
        "current_snapshot_id": current_snapshot_id,
        "error": error,
    }


def reference_rows(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Spark's own cells: the reference every reader is compared with."""
    return [
        matrix_row(
            t["table"],
            "spark",
            "reference",
            t["snapshot_id"],
            t["row_count"],
            t["table_digest"],
            t["snapshot_id"],
            None,
        )
        for t in job["tables"]
    ]


def cannot_load(table: Mapping[str, Any], reader: str, message: str) -> dict[str, Any]:
    """A cell for a table the reader could not load; the counts stay empty, never zero."""
    return matrix_row(
        table["table"], reader, "cannot-load", table["snapshot_id"], None, None, None, message
    )


def loaded(
    table: Mapping[str, Any],
    reader: str,
    row_count: int,
    digest: str,
    current_snapshot_id: int | None,
) -> dict[str, Any]:
    """A cell for a table the reader loaded, judged against Spark's reference."""
    return matrix_row(
        table["table"],
        reader,
        cell_result(table, row_count, digest),
        table["snapshot_id"],
        row_count,
        digest,
        current_snapshot_id,
        None,
    )


def lakekeeper_url() -> str:
    return os.environ.get("LAKEKEEPER_URL", DEFAULT_LAKEKEEPER_URL).rstrip("/")


def identifier(name: str) -> str:
    """A table or namespace name that is safe to put in SQL; the job's own names always are."""
    if not _IDENTIFIER.match(name):
        raise ValueError("not a plain SQL identifier")
    return name


def duckdb_connect() -> Any:
    """A DuckDB connection that loads the baked iceberg extension and never downloads one."""
    import duckdb

    con = duckdb.connect(
        config={
            "extension_directory": DUCKDB_EXTENSIONS,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": True,
        }
    )
    con.execute("LOAD iceberg")
    con.execute("SET TimeZone = 'UTC'")
    return con


def read_duckdb(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """DuckDB's cell for each table, in the job's table order.

    VARIANT is selected as `payload::JSON` (`to_json` double-encodes it and `::VARCHAR` prints
    struct notation); each read is pinned with AT (VERSION => <Spark's snapshot id>).
    """
    tables = job["tables"]
    namespace = identifier(str(job["namespace"]))
    try:
        con = duckdb_connect()
        con.execute(
            f"ATTACH '{WAREHOUSE}' AS lk (TYPE iceberg, "
            f"ENDPOINT '{lakekeeper_url()}/catalog', AUTHORIZATION_TYPE 'none')"
        )
    except Exception as exc:
        return [cannot_load(t, "duckdb", error_text(exc)) for t in tables]
    rows = []
    for table in tables:
        try:
            name = identifier(str(table["table"]))
            snapshot_id = int(table["snapshot_id"])
            payload = "payload::JSON" if table["payload_type"] == "variant" else "payload"
            arrow = con.execute(
                f"SELECT id, name, amount, ts, {payload} AS payload "
                f"FROM lk.{namespace}.{name} AT (VERSION => {snapshot_id})"
            ).to_arrow_table()
            count, digest = row_hash.arrow_table_digest(arrow, COLUMNS, JSON_COLUMNS)
            current = con.execute(
                f"SELECT snapshot_id FROM iceberg_snapshots(lk.{namespace}.{name}) "
                f"ORDER BY sequence_number DESC LIMIT 1"
            ).fetchone()
            rows.append(
                loaded(table, "duckdb", count, digest, int(current[0]) if current else None)
            )
        except Exception as exc:
            rows.append(cannot_load(table, "duckdb", error_text(exc)))
    return rows


def duckdb_extension_version() -> str:
    """The loaded iceberg extension's version, or the error text if it will not load."""
    try:
        con = duckdb_connect()
        found = con.execute(
            "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'iceberg'"
        ).fetchone()
        return str(found[0]) if found else "unknown"
    except Exception as exc:
        return error_text(exc)


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def reader_versions() -> dict[str, str]:
    return {
        "duckdb": package_version("duckdb"),
        "duckdb_iceberg_extension": duckdb_extension_version(),
    }


def build_matrix(
    job: Mapping[str, Any], reader_rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Spark's rows plus the readers', ordered by the job's table order and then READERS."""
    order = {t["table"]: i for i, t in enumerate(job["tables"])}
    cells = [*reference_rows(job), *reader_rows]
    return sorted(
        cells, key=lambda r: (order.get(r["table"], len(order)), READERS.index(r["reader"]))
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: read_v3.py <spark job JSON file>", file=sys.stderr)
        return 2
    try:
        job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"read_v3: cannot read the job file: {error_text(exc)}", file=sys.stderr)
        return 2
    if not isinstance(job, dict) or not isinstance(job.get("tables"), list):
        print("read_v3: the job file has no tables list", file=sys.stderr)
        return 2
    report: dict[str, Any] = {
        "versions": reader_versions(),
        "matrix": build_matrix(job, read_duckdb(job)),
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
