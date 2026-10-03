"""Item 5's write-audit-publish and rollback checks on bronze, run with Spark on the item 14 image.

Run inside the `spark-job` one-shot against the running streaming stack:
`python /app/bronze_wap.py brnz01-pre`. Every subcommand prints one compact JSON line, the last line
of stdout, and the next subcommand reads that line on stdin. Plan 04-03 uses it for ADR-001 go
criterion 5's second half and for the two rollback paths (Bronze rollback).

The pure parts are unit-tested: the SQL builders (`qualified`, `pk_predicate`, `erase_sql`,
`rewrite_sql`, `fast_forward_sql`, `expire_sql`, `replace_branch_sql`, `rollback_sql`,
`count_keys_sql`), `resume_offsets`, `pick_ledger`, `lineage_ops`, `written_by`, `guard_expire` and
`brnz_verdict`. The live steps need a running stack and have no unit tests (ADR-001 Evidence rules).
Heavy imports (pyspark, pyiceberg, pyarrow, confluent_kafka) sit inside functions, so importing this
module in CI is safe, and `verdict FILE...` runs on the host with no Spark.

Only Spark touches refs: DuckDB has no branches (G2). The erasure ledger is a list of synthetic
integer customer keys that stands in for Week 5's ledger. Nothing here prints a row, a name or an
address: every check prints counts, snapshot ids, operations and integer keys. Every table name
passes `read_v3.identifier` and `connect_admin.TABLES`, every key passes `int()`, and a branch comes
only from BRANCHES, so the SQL is built from constants.

The order is fixed by ADR-001: the erasure DELETE and the rewrite on `audit`, then the fast-forward,
then `expire_snapshots`. Expiry keeps every branch head, so running it before the fast-forward would
keep `main`'s pre-DELETE files, and it never runs while the sink is running: `guard_expire` refuses.
`remove_orphan_files` is not run (INV-07 keeps it off until ADR-002 names an owner).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import cdc_check
import connect_admin
from read_v3 import error_text, identifier
from spark_v3_job import CATALOG, spark_session

LEDGER_TABLE = "customers"
BRANCHES = ("audit", "main")
NAMESPACE = cdc_check.NAMESPACE
SINK = connect_admin.SINK_NAME
SINK_PROP_PREFIX = "kafka.connect."
CATCH_UP_TIMEOUT_S = 300.0
CATCH_UP_INTERVAL_S = 10.0
AFTER_TIMEOUT_S = 240.0
ERASING_OPERATIONS = ("delete", "overwrite")


# --- pure SQL builders ------------------------------------------------------------------------


def bronze_table(table: str) -> str:
    """`table` when it is one of the five bronze tables; ValueError for anything else."""
    name = identifier(table)
    if name not in connect_admin.TABLES:
        raise ValueError("not a bronze table")
    return name


def checked_branch(branch: str) -> str:
    if branch not in BRANCHES:
        raise ValueError("branch must be audit or main")
    return branch


def qualified(table: str, branch: str | None = None) -> str:
    """`rest.bronze.<t>`, or `rest.bronze.<t>.branch_audit` for the audit branch; main is the plain name."""
    name = f"{CATALOG}.{NAMESPACE}.{bronze_table(table)}"
    if branch is None or checked_branch(branch) == "main":
        return name
    return f"{name}.branch_audit"


def pk_predicate(table: str, keys: Sequence[int]) -> str:
    """`coalesce(after.<pk>, before.<pk>) IN (<ints>)` for a single-column primary key.

    A delete's `after` is null, so the key comes from `before`. An empty list, a non-integer or a
    composite-key table raises ValueError. The keys are sorted and unique.
    """
    columns = cdc_check.PK_COLUMNS[bronze_table(table)]
    if len(columns) != 1:
        raise ValueError(f"{table} has a composite primary key")
    wanted = list(keys)
    if not wanted:
        raise ValueError("the ledger is empty")
    if any(isinstance(key, bool) or not isinstance(key, int) for key in wanted):
        raise ValueError("every ledger key must be an integer")
    column = identifier(columns[0])
    values = ", ".join(str(int(key)) for key in sorted(set(wanted)))
    return f"coalesce(after.{column}, before.{column}) IN ({values})"


def erase_sql(table: str, keys: Sequence[int], branch: str) -> str:
    return f"DELETE FROM {qualified(table, branch)} WHERE {pk_predicate(table, keys)}"


def rewrite_sql(table: str, branch: str) -> str:
    """rewrite_data_files on `branch`, rewriting every file in a group of at least two."""
    return (
        f"CALL {CATALOG}.system.rewrite_data_files(table => '{NAMESPACE}.{bronze_table(table)}', "
        f"branch => '{checked_branch(branch)}', "
        f"options => map('rewrite-all', 'true', 'min-input-files', '2'))"
    )


def fast_forward_sql(table: str) -> str:
    """Move main to audit's head; an absent main is created there, an existing one must be an ancestor."""
    return (
        f"CALL {CATALOG}.system.fast_forward('{NAMESPACE}.{bronze_table(table)}', 'main', 'audit')"
    )


def expire_sql(table: str, older_than: datetime) -> str:
    """expire_snapshots up to `older_than` (an aware datetime, rendered in UTC), keeping one snapshot."""
    if older_than.tzinfo is None or older_than.utcoffset() is None:
        raise ValueError("older_than must be timezone-aware")
    stamp = older_than.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    return (
        f"CALL {CATALOG}.system.expire_snapshots(table => '{NAMESPACE}.{bronze_table(table)}', "
        f"older_than => TIMESTAMP '{stamp}', retain_last => 1)"
    )


def snapshot_literal(snapshot_id: int) -> int:
    if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int):
        raise ValueError("a snapshot id must be an integer")
    return snapshot_id


def replace_branch_sql(table: str, snapshot_id: int) -> str:
    """Recreate audit at `snapshot_id` (main's head), dropping every commit after it."""
    return (
        f"ALTER TABLE {qualified(table)} CREATE OR REPLACE BRANCH audit "
        f"AS OF VERSION {snapshot_literal(snapshot_id)}"
    )


def rollback_sql(table: str, snapshot_id: int) -> str:
    return (
        f"CALL {CATALOG}.system.rollback_to_snapshot('{NAMESPACE}.{bronze_table(table)}', "
        f"{snapshot_literal(snapshot_id)})"
    )


def count_keys_sql(table: str, keys: Sequence[int], ref: str | int) -> str:
    """Count the rows holding a ledger key at `ref`: a branch name (audit, main) or a snapshot id."""
    version = str(snapshot_literal(ref)) if isinstance(ref, int) else f"'{checked_branch(ref)}'"
    return (
        f"SELECT count(*) FROM {qualified(table)} VERSION AS OF {version} "
        f"WHERE {pk_predicate(table, keys)}"
    )


# --- pure rules -------------------------------------------------------------------------------


def resume_offsets(
    rows_max: Mapping[tuple[str, int], int],
    partitions: Sequence[tuple[str, int]],
    log_start: Mapping[tuple[str, int], int],
) -> dict[tuple[str, int], int]:
    """Per source partition: one past the highest offset among the restored rows, else log start.

    `rows_max` maps (topic, partition) to the highest `_kafka_metadata_offset` among the rows of the
    snapshot being restored; `partitions` is every partition the connector reads. A rows key outside
    `partitions`, or a partition with neither rows nor a log-start offset, raises ValueError.
    """
    known = set(partitions)
    stray = sorted(set(rows_max) - known)
    if stray:
        raise ValueError(f"rows exist for a partition that was not given: {stray[0]}")
    resume: dict[tuple[str, int], int] = {}
    for key in sorted(known):
        if key in rows_max:
            resume[key] = int(rows_max[key]) + 1
        elif key in log_start:
            resume[key] = int(log_start[key])
        else:
            raise ValueError(f"no rows and no log-start offset for partition {key}")
    return resume


def pick_ledger(rows: Iterable[tuple[int, int, int]], older: int = 2) -> list[int]:
    """The stand-in erasure ledger: synthetic integer keys from (partition, offset, pk) rows.

    Each partition's highest-offset key (so a replay re-lands it, research P8) plus the `older`
    lowest-offset keys not already chosen. Sorted and unique.
    """
    ordered = sorted(rows, key=lambda row: (row[1], row[0]))
    top: dict[int, tuple[int, int]] = {}
    for partition, offset, pk in ordered:
        if partition not in top or offset >= top[partition][0]:
            top[partition] = (offset, pk)
    chosen = {pk for _offset, pk in top.values()}
    added = 0
    for _partition, _offset, pk in ordered:
        if added >= older:
            break
        if pk not in chosen:
            chosen.add(pk)
            added += 1
    return sorted(chosen)


def written_by(summary_keys: Iterable[str]) -> str:
    """`sink` when a snapshot summary carries a `kafka.connect.` key, else `spark`."""
    return "sink" if any(key.startswith(SINK_PROP_PREFIX) for key in summary_keys) else "spark"


def lineage_ops(
    snapshots: Mapping[int, tuple[int | None, str]], head: int, base: int | None
) -> list[str]:
    """The operations of the snapshots after `base` up to `head`, oldest first.

    `snapshots` maps a snapshot id to (parent id, operation). `base` None walks to the root. A base
    that is not an ancestor of `head` raises ValueError.
    """
    ops: list[str] = []
    current: int | None = head
    while current is not None and current != base:
        parent, operation = snapshots[current]
        ops.append(operation)
        current = parent
    if base is not None and current != base:
        raise ValueError("base is not an ancestor of head")
    return list(reversed(ops))


def guard_expire(executed: Sequence[str], sink_stopped: bool) -> None:
    """Refuse expire_snapshots before the fast-forward or while the sink runs (ADR-001, P9).

    Expiry keeps every branch head, so expiring before the fast-forward would keep main's pre-DELETE
    files; a sink commit round in flight during expiry could lose its snapshot.
    """
    if "fast_forward" not in executed:
        raise RuntimeError("expire_snapshots must come after the fast-forward")
    if not sink_stopped:
        raise RuntimeError("expire_snapshots must not run while the sink is running")


def last_json_object(text: str) -> dict[str, Any] | None:
    """The last line of `text` that is a JSON object, or None."""
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def ledger_of(report: Mapping[str, Any]) -> list[int]:
    """The ledger of a saved step report: a list of integers, never anything else."""
    keys = report.get("ledger")
    if not isinstance(keys, list) or not keys:
        raise ValueError("the step report has no ledger")
    if not all(isinstance(key, int) and not isinstance(key, bool) for key in keys):
        raise ValueError("the ledger must hold integers only")
    return [int(key) for key in keys]


def brnz_verdict(steps: Mapping[str, Mapping[str, Any] | None]) -> dict[str, Any]:
    """Item 5's write-audit-publish verdict: go, fallback ("no branch commit") or inconclusive.

    Tracer version: the rollback steps are judged by Task 2's rule.
    """
    return {"verdict": "inconclusive", "fallback": None, "reasons": ["rollback steps not run"]}


# --- live: refs and files ---------------------------------------------------------------------


def snapshot_facts(iceberg_table: Any, snapshot_id: int) -> dict[str, str]:
    """{"operation", "written_by"} of one snapshot; written_by is sink or spark."""
    snapshot = iceberg_table.snapshot_by_id(snapshot_id)
    summary = snapshot.summary if snapshot is not None else None
    if summary is None:
        return {"operation": "unknown", "written_by": "spark"}
    properties = getattr(summary, "additional_properties", None) or {}
    operation = getattr(summary.operation, "value", str(summary.operation))
    return {"operation": str(operation).lower(), "written_by": written_by(properties)}


def snapshot_lineage(iceberg_table: Any) -> dict[int, tuple[int | None, str]]:
    """Every snapshot's (parent id, operation), keyed by snapshot id."""
    lineage: dict[int, tuple[int | None, str]] = {}
    for snapshot in iceberg_table.snapshots():
        parent = snapshot.parent_snapshot_id
        facts = snapshot_facts(iceberg_table, int(snapshot.snapshot_id))
        lineage[int(snapshot.snapshot_id)] = (
            int(parent) if parent is not None else None,
            facts["operation"],
        )
    return lineage


def refs_report() -> dict[str, dict[str, Any]]:
    """Per table: main and audit snapshot ids (null when absent), the snapshot count and each head's facts."""
    report: dict[str, dict[str, Any]] = {}
    for table in connect_admin.TABLES:
        iceberg_table = cdc_check.load_bronze(table)
        found = {name: int(ref.snapshot_id) for name, ref in iceberg_table.refs().items()}
        report[table] = {
            "main": found.get("main"),
            "audit": found.get("audit"),
            "snapshots": len(iceberg_table.snapshots()),
            "heads": {
                name: snapshot_facts(iceberg_table, found[name])
                for name in BRANCHES
                if name in found
            },
        }
    return report


def audit_ops_since(table: str, base: int) -> list[str]:
    """The operations on `table`'s audit branch after snapshot `base`, oldest first."""
    iceberg_table = cdc_check.load_bronze(table)
    head = int(iceberg_table.refs()["audit"].snapshot_id)
    return lineage_ops(snapshot_lineage(iceberg_table), head, base)


def run(spark: Any, sql: str) -> list[dict[str, Any]]:
    """Run one Spark statement and return its result rows as dicts (a procedure's counts, never data)."""
    return [row.asDict() for row in spark.sql(sql).collect()]


def count_keys(spark: Any, table: str, keys: Sequence[int], ref: str | int) -> int:
    (row,) = run(spark, count_keys_sql(table, keys, ref))
    return int(next(iter(row.values())))


def fast_forward_all(spark: Any) -> dict[str, str]:
    """Fast-forward main to audit on all five tables: {table: "ok" or the error's first line}."""
    outcome: dict[str, str] = {}
    for table in connect_admin.TABLES:
        try:
            run(spark, fast_forward_sql(table))
            outcome[table] = "ok"
        except Exception as exc:
            outcome[table] = error_text(exc)
    return outcome


def files_check(spark: Any, table: str, keys: Sequence[int]) -> dict[str, int]:
    """Open every data file any snapshot references and count the rows holding a ledger key.

    The files are listed from `all_data_files` and opened through the PyIceberg table's FileIO
    (vended credentials, no static key). Only counts leave: {"files", "rows_matching"}.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    column = cdc_check.PK_COLUMNS[bronze_table(table)][0]
    paths = sorted(
        {
            str(row["file_path"])
            for row in run(spark, f"SELECT file_path FROM {qualified(table)}.all_data_files")
        }
    )
    iceberg_table = cdc_check.load_bronze(table)
    wanted = pa.array(sorted({int(key) for key in keys}), pa.int64())
    matching = 0
    for path in paths:
        with iceberg_table.io.new_input(path).open() as handle:
            parquet = pq.ParquetFile(handle)
            present = [name for name in ("after", "before") if name in parquet.schema_arrow.names]
            data = parquet.read(columns=present)
        ids = None
        for name in present:
            part = pc.struct_field(data[name], column)
            ids = part if ids is None else pc.coalesce(ids, part)
        if ids is not None:
            hits = pc.is_in(pc.cast(ids, pa.int64()), value_set=wanted)
            matching += int(pc.sum(pc.cast(hits, pa.int64())).as_py() or 0)
    return {"files": len(paths), "rows_matching": matching}


def sink_stop() -> None:
    """Stop the sink and wait for STOPPED with no tasks: no commit round can be in flight."""
    connect_admin.stop(SINK)
    connect_admin.wait_stopped(SINK)


def sink_start() -> None:
    connect_admin.resume(SINK)
    connect_admin.wait_running(SINK)


def wait_set_equal(
    ref: str, allowed: frozenset[cdc_check.Offset], timeout_s: float = CATCH_UP_TIMEOUT_S
) -> dict[str, Any]:
    """Re-read Kafka and bronze at `ref` every 10 s until nothing is missing but allowed offsets.

    On a timeout the last summary is returned, so a real gap is reported rather than hidden.
    """
    deadline = time.monotonic() + timeout_s
    polls = 0
    while True:
        data = cdc_check.gather(ref)
        summary = cdc_check.summarize(data, 0, allowed)
        polls += 1
        caught_up = (
            not data["unreadable"]
            and summary["kafka_nonnull"] > 0
            and summary["missing"] <= summary["missing_allowed"]
        )
        if caught_up or time.monotonic() >= deadline:
            return {
                "caught_up": caught_up,
                "polls": polls,
                "missing": summary["missing"],
                "missing_allowed": summary["missing_allowed"],
            }
        time.sleep(CATCH_UP_INTERVAL_S)


# --- live: BRNZ-01 ----------------------------------------------------------------------------


def brnz01_pre() -> dict[str, Any]:
    """Refs before any fast-forward, the ledger, then the first fast-forward on all five tables."""
    before = refs_report()
    found = cdc_check.bronze_rows(LEDGER_TABLE, "audit")
    ledger = pick_ledger(
        (int(row["partition"]), int(row["offset"]), int(row["pk"][0]))
        for row in found["rows"]
        if row["pk"] is not None
    )
    spark = spark_session()
    try:
        ff1 = fast_forward_all(spark)
    finally:
        spark.stop()
    after = refs_report()
    return {
        "refs_before": before,
        "ledger": ledger,
        "ff1": ff1,
        "main_ids": {table: after[table]["main"] for table in connect_admin.TABLES},
    }


def brnz01_chain(pre: Mapping[str, Any]) -> dict[str, Any]:
    """Erase and compact on audit with the sink stopped, publish, expire, check files, resume."""
    ledger = ledger_of(pre)
    main_ids: Mapping[str, int] = pre["main_ids"]
    executed: list[str] = []
    catch_up = wait_set_equal("audit", frozenset())
    sink_stop()
    sink_stopped = True
    executed.append("stop")
    start = refs_report()
    base = int(start[LEDGER_TABLE]["audit"])
    main_unchanged = all(start[table]["main"] == main_ids[table] for table in connect_admin.TABLES)
    audit_advanced = base != main_ids[LEDGER_TABLE]
    spark = spark_session()
    try:
        run(spark, erase_sql(LEDGER_TABLE, ledger, "audit"))
        executed.append("erase")
        rewrite = run(spark, rewrite_sql(LEDGER_TABLE, "audit"))[0]
        executed.append("rewrite")
        middle = refs_report()
        main_unchanged = main_unchanged and all(
            middle[table]["main"] == main_ids[table] for table in connect_admin.TABLES
        )
        ops = audit_ops_since(LEDGER_TABLE, base)
        before_publish = cdc_check.set_check("audit", ledger)
        executed.append("set_check")
        ff2 = fast_forward_all(spark)
        executed.append("fast_forward")
        rows_main = count_keys(spark, LEDGER_TABLE, ledger, "main")
        rows_audit = count_keys(spark, LEDGER_TABLE, ledger, "audit")
        run(spark, erase_sql(LEDGER_TABLE, ledger, "audit"))
        repeat_erase_rows = rows_audit - count_keys(spark, LEDGER_TABLE, ledger, "audit")
        try:
            run(spark, fast_forward_sql(LEDGER_TABLE))
            repeat_ff = "ok"
        except Exception as exc:
            repeat_ff = error_text(exc)
        executed.append("repeat")
        guard_expire(executed, sink_stopped)
        expire = run(spark, expire_sql(LEDGER_TABLE, datetime.now(UTC)))[0]
        executed.append("expire")
        files = files_check(spark, LEDGER_TABLE, ledger)
        final = refs_report()
    finally:
        spark.stop()
    sink_start()
    sink_stopped = False
    executed.append("resume")
    return {
        "ledger": ledger,
        "catch_up_before_stop": catch_up,
        "main_unchanged_before_ff2": main_unchanged,
        "audit_advanced": audit_advanced,
        "audit_ops_added": ops,
        "rewrite": rewrite,
        "set_check": before_publish,
        "ff2": ff2,
        "rows_main": rows_main,
        "rows_audit": rows_audit,
        "repeat_erase_rows": repeat_erase_rows,
        "repeat_ff": repeat_ff,
        "expire": expire,
        "files_check": files,
        "snapshots_after": {LEDGER_TABLE: final[LEDGER_TABLE]["snapshots"]},
        "heads_after": {table: final[table]["heads"] for table in connect_admin.TABLES},
        "main_ids_after": {table: final[table]["main"] for table in connect_admin.TABLES},
        "audit_head_after": final[LEDGER_TABLE]["audit"],
        "resumed": True,
        "order": executed,
    }


def brnz01_after(chain: Mapping[str, Any]) -> dict[str, Any]:
    """Wait for a sink commit on customers' audit after expiry; main must still sit where the chain left it."""
    deadline = time.monotonic() + AFTER_TIMEOUT_S
    while True:
        report = refs_report()
        head = report[LEDGER_TABLE]["heads"].get("audit", {})
        newer = report[LEDGER_TABLE]["audit"] != chain["audit_head_after"]
        committed = bool(newer and head.get("written_by") == "sink")
        if committed or time.monotonic() >= deadline:
            break
        time.sleep(CATCH_UP_INTERVAL_S)
    return {
        "sink_commit_after_expiry": committed,
        "main_unchanged": all(
            report[table]["main"] == chain["main_ids_after"][table]
            for table in connect_admin.TABLES
        ),
        "audit_head": head,
    }


# --- CLI --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bronze write-audit-publish and rollback checks.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("brnz01-pre", help="refs, ledger and the first fast-forward")
    commands.add_parser(
        "brnz01-chain", help="erase, compact, publish, expire and check (stdin: pre)"
    )
    commands.add_parser("brnz01-after", help="the sink's commit after expiry (stdin: chain)")
    judged = commands.add_parser("verdict", help="judge saved step files (host-safe, no Spark)")
    judged.add_argument("files", nargs="+")
    return parser


def read_stdin_report() -> dict[str, Any]:
    report = last_json_object(sys.stdin.read())
    if report is None:
        raise ValueError("no JSON line on stdin")
    return report


STEP_NAMES = ("pre", "chain", "after", "path_a", "path_b")


def verdict_command(files: Sequence[str]) -> int:
    """Print brnz_verdict over the saved step files in STEP_NAMES order; exit 0 only for go."""
    steps: dict[str, Mapping[str, Any] | None] = {}
    for name, path in zip(STEP_NAMES, files, strict=False):
        try:
            with open(path, encoding="utf-8") as handle:
                steps[name] = last_json_object(handle.read())
        except OSError:
            steps[name] = None
    result = brnz_verdict(steps)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["verdict"] == "go" else 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "verdict":
            return verdict_command(args.files)
        if args.command == "brnz01-pre":
            report = brnz01_pre()
        elif args.command == "brnz01-chain":
            report = brnz01_chain(read_stdin_report())
        else:
            report = brnz01_after(read_stdin_report())
        print(json.dumps(report, sort_keys=True, default=str), flush=True)
    except Exception as exc:
        print(f"error: {error_text(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
