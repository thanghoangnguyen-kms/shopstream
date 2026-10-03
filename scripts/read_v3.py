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
# Values shorter than this are not scrubbed: a storage option such as `true` would blank ordinary
# words, and no vended key, secret or session token is this short.
MIN_SECRET_CHARS = 8
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# ADR-001 spike item 2's two fixed fallbacks, in its own wording.
FALLBACK_JSON = "JSON string column"
FALLBACK_V2 = "v2 with position deletes"


def error_text(exc: BaseException, secrets: Sequence[str] = ()) -> str:
    """The exception's type name, a colon and the first line of its message, at most 200 chars.

    Every value in `secrets` is scrubbed from the first line before it is truncated, so a key that
    straddles the 200-character cut leaks no prefix (WR-01).
    """
    lines = str(exc).splitlines()
    first = scrub_values(lines[0] if lines else "", secrets)
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


def polars_storage_options(properties: Mapping[str, str]) -> dict[str, str]:
    """Polars' object-store options from PyIceberg's `table.io.properties`.

    Polars does not reuse PyIceberg's vended credentials; without these it falls through to the
    instance-metadata endpoint and hangs. The result holds credentials: keep it in a local
    variable, never print or log it.
    """
    return {
        "aws_access_key_id": properties["s3.access-key-id"],
        "aws_secret_access_key": properties["s3.secret-access-key"],
        "aws_session_token": properties["s3.session-token"],
        "aws_endpoint_url": properties["s3.endpoint"].removesuffix("/"),
        "aws_region": properties["s3.region"],
        "aws_allow_http": "true",
    }


def scrub_values(text: str, values: Sequence[str]) -> str:
    """`text` with every value in `values` replaced, so no credential reaches output.

    Values shorter than MIN_SECRET_CHARS (including the empty string) are left alone.
    """
    for value in values:
        if len(value) >= MIN_SECRET_CHARS:
            text = text.replace(value, "<redacted>")
    return text


# The table that isolates each feature (see the job's design): d_json has no VARIANT and no
# deletes, b_json_dv adds deletion vectors, a_variant adds VARIANT, c_variant_dv has both.
BASE_TABLE = "d_json"
DELETES_TABLE = "b_json_dv"
VARIANT_TABLE = "a_variant"
BOTH_TABLE = "c_variant_dv"
EXPECTED_TABLES = (VARIANT_TABLE, DELETES_TABLE, BOTH_TABLE, BASE_TABLE)
TABLES_WITH_DELETES = (DELETES_TABLE, BOTH_TABLE)
TABLES_WITHOUT_DELETES = (VARIANT_TABLE, BASE_TABLE)
CHECKED_READERS = READERS[1:]


def spark_writer_findings(tables: Mapping[str, Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Check the writer's own evidence: (problems that stop the verdict, deletion-vector gaps).

    A digest that differs across the tables, or delete files on a table that should have none,
    make a later mismatch unattributable. Format 3 on all four tables and an added-dvs snapshot
    plus a PUFFIN delete file on b_json_dv and c_variant_dv are the deletion-vector evidence;
    without it Spark itself takes ADR-001's `v2 with position deletes` fallback.
    """
    problems = []
    if len({tables[name]["table_digest"] for name in EXPECTED_TABLES}) != 1:
        problems.append("Spark's table digests differ across the four tables")
    for name in TABLES_WITHOUT_DELETES:
        if tables[name].get("delete_files"):
            problems.append(f"{name} has delete files, so a mismatch could not be attributed")
    gaps = []
    for name in EXPECTED_TABLES:
        if tables[name].get("format_version") != 3:
            gaps.append(f"{name} is format-version {tables[name].get('format_version')}, not 3")
    for name in TABLES_WITH_DELETES:
        table = tables[name]
        if not any(s.get("added_dvs", 0) >= 1 for s in table.get("snapshots", [])):
            gaps.append(f"no snapshot of {name} has added-dvs of 1 or more")
        if not any(
            f.get("content") == 1 and f.get("file_format") == "PUFFIN"
            for f in table.get("delete_files", [])
        ):
            gaps.append(f"{name} has no PUFFIN delete file")
    return problems, gaps


def reader_findings(reader: str, results: Mapping[str, str]) -> tuple[str | None, list[str], str]:
    """Attribute one reader's four results: (problem or None, fallbacks it needs, a reason line).

    d_json is the base read: a reader failing it fails for a reason neither rule explains.
    c_variant_dv has both features, so it must match exactly when a_variant and b_json_dv do.
    """
    shown = ", ".join(f"{name} {results[name]}" for name in EXPECTED_TABLES)
    if results[BASE_TABLE] != "match":
        return (
            f"{reader} does not match Spark on {BASE_TABLE}, the plain table with no VARIANT and "
            f"no deletes ({shown}), so no failure can be attributed",
            [],
            "",
        )
    variant_ok = results[VARIANT_TABLE] == "match"
    deletes_ok = results[DELETES_TABLE] == "match"
    both_ok = results[BOTH_TABLE] == "match"
    if both_ok != (variant_ok and deletes_ok):
        return (
            f"{reader}'s results ({shown}) fit neither a VARIANT failure nor a deletion-vector "
            f"failure",
            [],
            "",
        )
    if variant_ok and deletes_ok:
        return None, [], f"{reader} matches Spark on all four tables"
    fallbacks = []
    parts = []
    if not variant_ok:
        fallbacks.append(FALLBACK_JSON)
        parts.append(
            f"cannot take the VARIANT tables ({shown}), so it reads the payload as a "
            f"{FALLBACK_JSON}"
        )
    if not deletes_ok:
        fallbacks.append(FALLBACK_V2)
        parts.append(f"does not apply the deletion vectors ({shown}), so it takes {FALLBACK_V2}")
    return None, fallbacks, f"{reader} " + "; ".join(parts)


def item2_verdict(job: Mapping[str, Any], matrix: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Map the parity matrix onto ADR-001 item 2's two fixed fallbacks.

    Returns `verdict` (go, fallback or inconclusive), `fallbacks` (reader and fallback, only
    ever ADR-001's two wordings) and `reasons`. Anything the two rules cannot attribute, or any
    missing cell, is inconclusive with no fallback named: the owner decides.
    """
    tables = {str(t["table"]): t for t in job["tables"]}
    cells = {(str(c["table"]), str(c["reader"])): c for c in matrix}
    problems = [f"the job has no {name} table" for name in EXPECTED_TABLES if name not in tables]
    problems += [
        f"the matrix has no {reader} cell for {name}"
        for name in EXPECTED_TABLES
        for reader in READERS
        if (name, reader) not in cells
    ]
    if problems:
        return {"verdict": "inconclusive", "fallbacks": [], "reasons": problems}
    writer_problems, deletion_vector_gaps = spark_writer_findings(tables)
    problems += writer_problems
    fallbacks: list[dict[str, str]] = []
    reasons: list[str] = []
    if deletion_vector_gaps:
        fallbacks.append({"reader": "spark", "fallback": FALLBACK_V2})
        reasons.append(
            "Spark lacks deletion-vector evidence (" + "; ".join(deletion_vector_gaps) + f"), "
            f"so it takes {FALLBACK_V2}"
        )
    else:
        reasons.append(
            "Spark wrote format-version 3 on all four tables, with added-dvs and PUFFIN delete "
            f"files on {DELETES_TABLE} and {BOTH_TABLE}"
        )
    for reader in CHECKED_READERS:
        results = {name: str(cells[(name, reader)]["result"]) for name in EXPECTED_TABLES}
        problem, needed, reason = reader_findings(reader, results)
        if problem:
            problems.append(problem)
            continue
        fallbacks += [{"reader": reader, "fallback": fallback} for fallback in needed]
        reasons.append(reason)
    if problems:
        return {"verdict": "inconclusive", "fallbacks": [], "reasons": problems}
    return {
        "verdict": "fallback" if fallbacks else "go",
        "fallbacks": fallbacks,
        "reasons": reasons,
    }


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


def pyiceberg_tables(job: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], Any, str | None]]:
    """Load the catalog once, then each table: (job table, PyIceberg table or None, error or None).

    A table with a VARIANT column fails here with a ValidationError ("Unsupported field type:
    'variant'"): PyIceberg 0.12.0 has no VariantType, so the whole table is unreadable. The vended
    credentials stay inside the returned table objects.
    """
    from pyiceberg.catalog import load_catalog

    namespace = identifier(str(job["namespace"]))
    try:
        catalog = load_catalog(
            "rest",
            uri=f"{lakekeeper_url()}/catalog",
            warehouse=WAREHOUSE,
            **{"header.X-Iceberg-Access-Delegation": "vended-credentials"},
        )
    except Exception as exc:
        return [(t, None, error_text(exc)) for t in job["tables"]]
    found: list[tuple[Mapping[str, Any], Any, str | None]] = []
    for table in job["tables"]:
        try:
            name = identifier(str(table["table"]))
            found.append((table, catalog.load_table(f"{namespace}.{name}"), None))
        except Exception as exc:
            found.append((table, None, error_text(exc)))
    return found


def current_snapshot_id(iceberg_table: Any) -> int | None:
    snapshot = iceberg_table.current_snapshot()
    return int(snapshot.snapshot_id) if snapshot is not None else None


def read_pyiceberg(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """PyIceberg's cell for each table: `scan(snapshot_id=...)` at Spark's snapshot id."""
    rows = []
    for table, iceberg_table, error in pyiceberg_tables(job):
        if iceberg_table is None:
            rows.append(cannot_load(table, "pyiceberg", error or "unknown error"))
            continue
        try:
            arrow = iceberg_table.scan(
                snapshot_id=int(table["snapshot_id"]), selected_fields=COLUMNS
            ).to_arrow()
            count, digest = row_hash.arrow_table_digest(arrow, COLUMNS, JSON_COLUMNS)
            rows.append(
                loaded(table, "pyiceberg", count, digest, current_snapshot_id(iceberg_table))
            )
        except Exception as exc:
            rows.append(cannot_load(table, "pyiceberg", error_text(exc)))
    return rows


def read_polars(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Polars' cell for each table: `scan_iceberg(table, snapshot_id=...)` with storage options.

    Polars loads tables through PyIceberg, so a table PyIceberg cannot load is cannot-load here
    too, and the error says so. The storage options hold the vended credentials: they stay in a
    local variable, and any error text is scrubbed of their values before it is truncated.
    """
    import polars as pl

    rows = []
    for table, iceberg_table, error in pyiceberg_tables(job):
        if iceberg_table is None:
            message = f"PyIceberg load failed ({error})"[:ERROR_LIMIT]
            rows.append(cannot_load(table, "polars", message))
            continue
        options: dict[str, str] = {}
        try:
            options = polars_storage_options(iceberg_table.io.properties)
            frame = (
                pl.scan_iceberg(
                    iceberg_table, snapshot_id=int(table["snapshot_id"]), storage_options=options
                )
                .select(list(COLUMNS))
                .collect()
            )
            count, digest = row_hash.arrow_table_digest(frame.to_arrow(), COLUMNS, JSON_COLUMNS)
            rows.append(loaded(table, "polars", count, digest, current_snapshot_id(iceberg_table)))
        except Exception as exc:
            message = error_text(exc, list(options.values()))
            rows.append(cannot_load(table, "polars", message))
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
        "pyiceberg": package_version("pyiceberg"),
        "polars": package_version("polars"),
        "pyarrow": package_version("pyarrow"),
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
    reader_rows = [*read_duckdb(job), *read_pyiceberg(job), *read_polars(job)]
    matrix = build_matrix(job, reader_rows)
    verdict = item2_verdict(job, matrix)
    report: dict[str, Any] = {"versions": reader_versions(), "matrix": matrix, **verdict}
    print(json.dumps(report, ensure_ascii=False))
    return 1 if verdict["verdict"] == "inconclusive" else 0


if __name__ == "__main__":
    sys.exit(main())
