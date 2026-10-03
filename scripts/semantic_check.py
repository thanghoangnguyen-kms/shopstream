"""Item 4's check: MetricFlow's rows equal hand-written SQL's rows over Iceberg gold.

Run inside the `dbt-job` one-shot with the MetricFlow venv's Python, with DBT_PROJECT_DIR and
DBT_PROFILES_DIR set to /work/analytics/dbt (they default to it here):
`/opt/mf/bin/python /app/semantic_check.py`. It runs, by absolute path only, `/opt/dbt/bin/dbt
--version`, `/opt/mf/bin/dbt --version`, `/opt/mf/bin/mf --version`, `/opt/mf/bin/dbt parse --target
lk`, `mf validate-configs` and two `mf query` calls (a metric grouped by day, and the ungrouped
total) that write `--csv` files. It then attaches the `spike` warehouse as `lk` in the venv's own
DuckDB, exactly as the dbt `lk` target does, and runs the two hand-written queries over
lk.gold.fct_orders. The two result sets are compared by day, ignoring row order, with values compared
as decimals (250 equals 250.00, 75 does not equal 75.01) and an empty result on either side a
mismatch, never a match. The last line of stdout is one compact JSON object: `versions`,
`dbt_parse`, `validate_configs`, `mf_rows`, `sql_rows`, `mf_total`, `sql_total`, `comparison`,
`errors`, `verdict` and `reasons`. It exits 0 for go or fallback and 1 for inconclusive.

`mf` reads `target/semantic_manifest.json`, which dbt-core writes (without it `mf` stops with "Unable
to load the semantic manifest"), and any other target's run overwrites it: a `--target ci` run on the
host made `mf` query "memory"."gold"."fct_orders". So the check first runs `dbt parse --target lk
--no-partial-parse`, which rewrites the artifact for the `lk` target without building anything.

This is a spike script (ADR-001 Evidence rules): the live part needs a stack and has no unit tests;
the parser, the comparison and the verdict rule do. Every heavy import (subprocess, duckdb) sits
inside a function, so importing this module in CI is safe.
"""

from __future__ import annotations

import csv
import importlib.metadata
import io
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

DBT_CLI = "/opt/dbt/bin/dbt"
MF_DBT_CLI = "/opt/mf/bin/dbt"
MF_CLI = "/opt/mf/bin/mf"
EXPECTED_DBT_CORE = "1.12."
DEFAULT_DBT_DIR = "/work/analytics/dbt"
TIMEOUT_S = 300
ERROR_LIMIT = 200
# Writes target/semantic_manifest.json for the `lk` target, which is what `mf` reads.
PARSE_ARGV = (
    MF_DBT_CLI,
    "parse",
    "--target",
    "lk",
    "--no-partial-parse",
    "--project-dir",
    DEFAULT_DBT_DIR,
    "--profiles-dir",
    DEFAULT_DBT_DIR,
)
HAND_SQL = (
    "SELECT order_date AS metric_time__day, SUM(amount) AS total_revenue "
    "FROM lk.gold.fct_orders GROUP BY 1 ORDER BY 1"
)
HAND_TOTAL_SQL = "SELECT SUM(amount) AS total_revenue FROM lk.gold.fct_orders"
# Fixed statements, so no SQL is built from strings. Extension signature checks stay on.
DUCKDB_SETUP = (
    "INSTALL iceberg",
    "LOAD iceberg",
    "INSTALL httpfs",
    "LOAD httpfs",
    "ATTACH 'spike' AS lk (TYPE iceberg, ENDPOINT 'http://lakekeeper:8181/catalog', "
    "AUTHORIZATION_TYPE 'none')",
)
_INSTALLED = re.compile(r"installed:\s*([0-9][0-9A-Za-z.+\-]*)")
_SUCCESSFUL = re.compile(r"Successfully")

Row = tuple[str | None, Decimal]


@dataclass(frozen=True)
class Comparison:
    """Two result sets keyed by day. `missing_days` are in the SQL only, `extra_days` in MetricFlow
    only; `differing` holds (day, MetricFlow value, SQL value)."""

    match: bool
    empty: bool
    missing_days: tuple[str | None, ...]
    extra_days: tuple[str | None, ...]
    differing: tuple[tuple[str | None, Decimal, Decimal], ...]
    duplicate_days: tuple[str | None, ...]


def parse_mf_csv(text: str) -> list[tuple[str | None, str]]:
    """`mf query --csv` output as (day, value) pairs; the ungrouped one-column form has no day."""
    rows = [row for row in csv.reader(io.StringIO(text, newline="")) if row]
    if not rows:
        return []
    width = len(rows[0])
    if width not in {1, 2}:
        raise ValueError(f"expected 1 or 2 columns, the header has {width} column(s)")
    parsed: list[tuple[str | None, str]] = []
    for row in rows[1:]:
        if len(row) != width:
            raise ValueError(f"a row has {len(row)} column(s), the header has {width}")
        parsed.append((None, row[0]) if width == 1 else (row[0], row[1]))
    return parsed


def _iso_day(day: object) -> str | None:
    if day is None:
        return None
    if isinstance(day, datetime):
        stamp = day
    elif isinstance(day, date):
        return day.isoformat()
    else:
        text = str(day).strip()
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"not an ISO day: {text!r}") from exc
    if stamp.time() != time(0):
        raise ValueError(f"a day-grain value must sit at midnight, got {stamp.isoformat()}")
    return stamp.date().isoformat()


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    else:
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"not a number: {value!r}")
    return number


def normalise_rows(rows: Sequence[tuple[object, object]]) -> list[Row]:
    """Rows as (ISO day or None, Decimal), sorted by day. Midnight timestamps and dates give the
    same day; 250, 250.0 and 250.00 give equal Decimals."""
    normalised = [(_iso_day(day), _decimal(value)) for day, value in rows]
    return sorted(normalised, key=lambda row: (row[0] is not None, row[0] or ""))


def _sorted_keys(keys: set[str | None]) -> tuple[str | None, ...]:
    return tuple(sorted(keys, key=lambda key: (key is not None, key or "")))


def _duplicates(rows: Sequence[Row]) -> set[str | None]:
    seen: set[str | None] = set()
    repeated: set[str | None] = set()
    for day, _ in rows:
        if day in seen:
            repeated.add(day)
        seen.add(day)
    return repeated


def compare_rows(mf_rows: Sequence[Row], sql_rows: Sequence[Row]) -> Comparison:
    """Compare by day, ignoring order. An empty side or a repeated day is never a match."""
    mf = dict(mf_rows)
    sql = dict(sql_rows)
    missing = _sorted_keys(set(sql) - set(mf))
    extra = _sorted_keys(set(mf) - set(sql))
    differing = tuple(
        (day, mf[day], sql[day]) for day in _sorted_keys(set(mf) & set(sql)) if mf[day] != sql[day]
    )
    duplicates = _sorted_keys(_duplicates(mf_rows) | _duplicates(sql_rows))
    empty = not mf_rows or not sql_rows
    return Comparison(
        match=not (empty or missing or extra or differing or duplicates),
        empty=empty,
        missing_days=missing,
        extra_days=extra,
        differing=differing,
        duplicate_days=duplicates,
    )


def dbt_core_version(version_output: str) -> str | None:
    """The dbt-core version from `dbt --version` output (its `installed:` line), or None."""
    match = _INSTALLED.search(version_output)
    return match.group(1) if match else None


def _why_not_matching(label: str, comparison: Comparison) -> str:
    parts = []
    if comparison.empty:
        parts.append("a result is empty")
    if comparison.missing_days:
        parts.append(f"missing from MetricFlow: {list(comparison.missing_days)}")
    if comparison.extra_days:
        parts.append(f"only in MetricFlow: {list(comparison.extra_days)}")
    if comparison.differing:
        parts.append(f"{len(comparison.differing)} value(s) differ")
    if comparison.duplicate_days:
        parts.append(f"repeated: {list(comparison.duplicate_days)}")
    return f"{label} comparison does not match ({'; '.join(parts)})"


def item4_verdict(
    *,
    parse_exit: int = 0,
    validate_exit: int,
    query_exits: Sequence[int],
    comparisons: Sequence[Comparison],
    cli_versions: Mapping[str, str | None],
) -> tuple[str, list[str]]:
    """`fallback` when MetricFlow itself fails (validate-configs or a query exits non-zero),
    `inconclusive` when it ran but a comparison differs or a dbt CLI is not dbt-core 1.12.x, or when
    `dbt parse` failed before MetricFlow was judged, else `go`. Returns the verdict and its reasons."""
    if parse_exit != 0:
        return "inconclusive", [f"dbt parse exited {parse_exit}, so MetricFlow was not judged"]
    failed = []
    if validate_exit != 0:
        failed.append(f"mf validate-configs exited {validate_exit}")
    failed.extend(f"an mf query exited {code}" for code in query_exits if code != 0)
    if failed:
        return "fallback", [
            *failed,
            "MetricFlow itself failed: the Boring Semantic Layer is the fallback",
        ]
    reasons = []
    if not query_exits:
        reasons.append("no mf query ran")
    if not comparisons:
        reasons.append("no comparison was made")
    for index, comparison in enumerate(comparisons, start=1):
        if not comparison.match:
            reasons.append(_why_not_matching(f"query {index}", comparison))
    for cli in (DBT_CLI, MF_DBT_CLI):
        version = cli_versions.get(cli)
        if version is None:
            reasons.append(f"{cli} reported no dbt-core version")
        elif not version.startswith(EXPECTED_DBT_CORE):
            reasons.append(f"{cli} reported dbt-core {version}, not {EXPECTED_DBT_CORE}x")
    if reasons:
        return "inconclusive", reasons
    return "go", [
        "validate-configs and both mf queries succeeded",
        "MetricFlow's grouped and total rows equal the hand-written SQL's",
        f"both dbt CLIs report dbt-core {EXPECTED_DBT_CORE}x",
    ]


# --- the live part: needs the dbt-job container and a running stack ------------------------------


def _run(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
    """Run one fixed absolute-path command; return its exit code and stdout plus stderr."""
    import subprocess

    done = subprocess.run(
        list(argv), capture_output=True, text=True, env=dict(env), timeout=TIMEOUT_S, check=False
    )
    return done.returncode, done.stdout + done.stderr


def _error_text(exc: BaseException) -> str:
    lines = str(exc).splitlines()
    return f"{type(exc).__name__}: {lines[0] if lines else ''}"[:ERROR_LIMIT]


def _mf_query(extra: Sequence[str], env: Mapping[str, str]) -> tuple[int, list[Row], str | None]:
    """One `mf query` of total_revenue written to a CSV; exit code, normalised rows, parse error."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        target = os.path.join(scratch, "out.csv")
        code, _ = _run(
            [MF_CLI, "query", "--metrics", "total_revenue", *extra, "--csv", target], env
        )
        if code != 0 or not os.path.exists(target):
            return code, [], None
        with open(target, encoding="utf-8", newline="") as handle:
            text = handle.read()
    try:
        return code, normalise_rows(parse_mf_csv(text)), None
    except ValueError as exc:
        return code, [], _error_text(exc)


def _hand_sql() -> tuple[list[Row], list[Row]]:
    """Both hand-written queries over lk.gold.fct_orders, in the venv's own DuckDB."""
    import duckdb

    connection = duckdb.connect(":memory:")
    for statement in DUCKDB_SETUP:
        connection.execute(statement)
    grouped = normalise_rows(connection.execute(HAND_SQL).fetchall())
    total = normalise_rows(
        [(None, value) for (value,) in connection.execute(HAND_TOTAL_SQL).fetchall()]
    )
    return grouped, total


def _jsonable(rows: Sequence[Row]) -> list[list[str | None]]:
    return [[day, format(value, "f")] for day, value in rows]


def _comparison_json(comparison: Comparison) -> dict[str, object]:
    return {
        "match": comparison.match,
        "empty": comparison.empty,
        "missing_days": list(comparison.missing_days),
        "extra_days": list(comparison.extra_days),
        "differing": [
            [day, format(mf, "f"), format(sql, "f")] for day, mf, sql in comparison.differing
        ],
        "duplicate_days": list(comparison.duplicate_days),
    }


def main() -> int:
    env = dict(os.environ)
    env.setdefault("DBT_PROJECT_DIR", DEFAULT_DBT_DIR)
    env.setdefault("DBT_PROFILES_DIR", DEFAULT_DBT_DIR)
    errors: list[str] = []

    dbt_code, dbt_text = _run([DBT_CLI, "--version"], env)
    mf_dbt_code, mf_dbt_text = _run([MF_DBT_CLI, "--version"], env)
    _, mf_text = _run([MF_CLI, "--version"], env)
    cli_versions = {DBT_CLI: dbt_core_version(dbt_text), MF_DBT_CLI: dbt_core_version(mf_dbt_text)}
    mf_version = re.search(r"version\s+(\S+)", mf_text)
    versions = {
        "dbt_cli": cli_versions[DBT_CLI],
        "mf_dbt_cli": cli_versions[MF_DBT_CLI],
        "mf": mf_version.group(1) if mf_version else None,
        "metricflow": importlib.metadata.version("metricflow"),
        "dbt_metricflow": importlib.metadata.version("dbt-metricflow"),
        "duckdb": importlib.metadata.version("duckdb"),
    }
    if dbt_code != 0 or mf_dbt_code != 0:
        errors.append(f"a dbt --version call exited {dbt_code} and {mf_dbt_code}")

    parse_exit, _ = _run(PARSE_ARGV, env)
    validate_exit, validate_text = _run([MF_CLI, "validate-configs"], env)
    validate = {"exit": validate_exit, "successful_stages": len(_SUCCESSFUL.findall(validate_text))}

    grouped_exit, mf_grouped, grouped_error = _mf_query(
        ["--group-by", "metric_time__day", "--order", "metric_time__day"], env
    )
    total_exit, mf_total, total_error = _mf_query([], env)
    errors.extend(error for error in (grouped_error, total_error) if error)

    sql_grouped: list[Row] = []
    sql_total: list[Row] = []
    try:
        sql_grouped, sql_total = _hand_sql()
    except Exception as exc:
        errors.append(f"hand-written SQL failed: {_error_text(exc)}")

    comparisons = (compare_rows(mf_grouped, sql_grouped), compare_rows(mf_total, sql_total))
    verdict, reasons = item4_verdict(
        parse_exit=parse_exit,
        validate_exit=validate_exit,
        query_exits=(grouped_exit, total_exit),
        comparisons=comparisons,
        cli_versions=cli_versions,
    )
    print(
        json.dumps(
            {
                "versions": versions,
                "dbt_parse": {"exit": parse_exit},
                "validate_configs": validate,
                "mf_rows": _jsonable(mf_grouped),
                "sql_rows": _jsonable(sql_grouped),
                "mf_total": _jsonable(mf_total),
                "sql_total": _jsonable(sql_total),
                "comparison": {
                    "grouped": _comparison_json(comparisons[0]),
                    "total": _comparison_json(comparisons[1]),
                },
                "errors": errors,
                "verdict": verdict,
                "reasons": reasons,
            }
        )
    )
    return 1 if verdict == "inconclusive" else 0


if __name__ == "__main__":
    sys.exit(main())
