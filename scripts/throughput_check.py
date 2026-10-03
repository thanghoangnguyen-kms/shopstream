"""Item 12's measurement: commits, committed throughput, offset runs, the exact set check and the disk.

Runs in two places. Inside the `cdc-run` one-shot (spike image, scripts mounted at /app) it reads
bronze.clickstream's snapshot metadata and, later, the table's offset islands:
`python /app/throughput_check.py commits` and `wait-rows --expect N --timeout S`. On the host the
pure commands run over captured files. The last line of stdout is always one JSON line. Exit 0 on
success, 1 on an error, a timeout or an inconclusive verdict, 2 on usage. A failure prints
`error: ` and the exception type (and the first message line with the canary scrubbed) to stderr.

Everything pure is unit-tested: `commits_from_snapshots`, `committed_throughput` and `offset_runs`.
The live parts are exercised by the tracer and dry runs of Plan 05-02.

A commit is an append snapshot that added rows. A round that committed no data files writes no
snapshot (Coordinator), and a snapshot with `added-records` 0 or another operation is never a commit.
Committed throughput is the rows of commits 2..N over the time from commit 1 to commit N (ADR-001
go criterion 12), compared with the 14,000 events/s floor in integers (rows x 1000 against
14,000 x span in ms), so no float rounding decides a verdict. The displayed rate is rounded
half-even to one decimal for the evidence only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from typing import Any

import cdc_check
from read_v3 import error_text

TABLE = "clickstream"
TOPIC = "clickstream"
FLOOR_EVENTS_PER_S = 14_000
MIN_COMMITS = 5
MIN_EVENTS = 5_000_000
TARGET_EVENTS = 50_000_000
# The 50M projection must fit within 3/4 of the VM disk: 4 x projected <= 3 x disk.
DISK_SHARE = (3, 4)
FALLBACK = "Spark bulk-loads the Week 14 volume into its own bronze table; Kafka carries a smaller live stream"
POLL_S = 10.0
CANARY_ENV = "CANARY_TOKEN"


# --- pure rules ---------------------------------------------------------------------------------


def _operation(snapshot: Mapping[str, Any]) -> str:
    text = str(snapshot.get("operation", "")).lower()
    return text.rsplit(".", 1)[-1]


def _added_records(snapshot: Mapping[str, Any]) -> int:
    value = snapshot.get("added_records")
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def commits_from_snapshots(snapshots: Iterable[Mapping[str, Any]]) -> list[dict[str, int]]:
    """The commits among a table's snapshots, oldest first.

    A commit is an append snapshot with `added_records` above 0. The result is sorted by
    (sequence_number, timestamp_ms, snapshot_id) whatever order the metadata listed the snapshots
    in, so a zero-row round or a non-append snapshot is never a commit. Each item holds
    snapshot_id, sequence_number, committed_at_ms and added_records as ints.
    """
    kept = [s for s in snapshots if _operation(s) == "append" and _added_records(s) > 0]
    kept.sort(
        key=lambda s: (
            int(s.get("sequence_number", 0)),
            int(s.get("timestamp_ms", 0)),
            int(s.get("snapshot_id", 0)),
        )
    )
    return [
        {
            "snapshot_id": int(s.get("snapshot_id", 0)),
            "sequence_number": int(s.get("sequence_number", 0)),
            "committed_at_ms": int(s.get("timestamp_ms", 0)),
            "added_records": _added_records(s),
        }
        for s in kept
    ]


def committed_throughput(commits: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """{computable, commits, rows_2_to_n, span_ms, events_per_s, meets_floor} for commits oldest first.

    Not computable below two commits or at a zero (or negative) span. `meets_floor` is the integer
    test rows x 1000 >= 14,000 x span_ms; `events_per_s` is the same ratio rounded half-even to one
    decimal, for display only (a Fraction rounds exactly, with no float in the way).
    """
    count = len(commits)
    if count < 2:
        return _not_computable(count, 0, 0)
    rows = sum(int(c["added_records"]) for c in commits[1:])
    span_ms = int(commits[-1]["committed_at_ms"]) - int(commits[0]["committed_at_ms"])
    if span_ms <= 0:
        return _not_computable(count, rows, span_ms)
    rate = round(Fraction(rows * 1000, span_ms), 1)
    return {
        "computable": True,
        "commits": count,
        "rows_2_to_n": rows,
        "span_ms": span_ms,
        "events_per_s": float(rate),
        "meets_floor": rows * 1000 >= FLOOR_EVENTS_PER_S * span_ms,
    }


def _not_computable(count: int, rows: int, span_ms: int) -> dict[str, Any]:
    return {
        "computable": False,
        "commits": count,
        "rows_2_to_n": rows,
        "span_ms": span_ms,
        "events_per_s": None,
        "meets_floor": False,
    }


def offset_runs(offsets: Iterable[int]) -> tuple[list[list[int]], int]:
    """Half-open runs [start, stop) of an offset collection, and its duplicate count.

    Runs that touch merge ([0, 5) and [5, 9) become [0, 9)); a one-offset gap keeps two runs. An
    offset that appears more than once counts once as a duplicate, however often it repeats, and
    stays inside its run. The result is O(runs) however many records there are.
    """
    runs: list[list[int]] = []
    duplicates = 0
    previous: int | None = None
    counted = False
    for offset in sorted(offsets):
        if previous is not None and offset == previous:
            if not counted:
                duplicates += 1
                counted = True
            continue
        counted = False
        previous = offset
        if runs and offset == runs[-1][1]:
            runs[-1][1] = offset + 1
        else:
            runs.append([offset, offset + 1])
    return runs, duplicates


# --- live: bronze.clickstream's snapshot metadata (cdc-run) -------------------------------------


def snapshots() -> list[dict[str, Any]]:
    """The table's snapshots as plain dicts, or [] while the sink has not created the table."""
    from pyiceberg.exceptions import NoSuchTableError

    try:
        table = cdc_check.load_bronze(TABLE)
    except NoSuchTableError:
        return []
    found: list[dict[str, Any]] = []
    for snap in table.snapshots():
        summary = snap.summary
        props = dict(summary.additional_properties) if summary is not None else {}
        operation = getattr(getattr(summary, "operation", None), "value", "")
        found.append(
            {
                "snapshot_id": int(snap.snapshot_id),
                "sequence_number": int(snap.sequence_number or 0),
                "timestamp_ms": int(snap.timestamp_ms),
                "operation": str(operation),
                "added_records": int(props.get("added-records", 0)),
                "total_records": int(props.get("total-records", 0)),
            }
        )
    return found


def commits_report(found: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The `commits` command's JSON: the commits, their row total and the throughput over 2..N."""
    commits = commits_from_snapshots(found)
    return {
        "commits": commits,
        "committed_events": sum(c["added_records"] for c in commits),
        "throughput": committed_throughput(commits),
        "snapshots_total": len(found),
    }


def newest_total_records(found: Sequence[Mapping[str, Any]]) -> int:
    """`total-records` of the newest snapshot (greatest sequence number), 0 for none."""
    if not found:
        return 0
    newest = max(found, key=lambda s: (int(s["sequence_number"]), int(s["timestamp_ms"])))
    return int(newest["total_records"])


def wait_rows(expect: int, timeout_s: float, poll_s: float = POLL_S) -> dict[str, Any]:
    """Poll the snapshot metadata until the newest snapshot holds `expect` rows; raise on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        total = newest_total_records(snapshots())
        if total >= expect:
            return {"expected": expect, "total_records": total}
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"bronze.{TABLE} held {total} of {expect} rows after {timeout_s:g} s"
            )
        time.sleep(poll_s)


# --- CLI ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Item 12: commits, throughput and the set check.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("commits", help="the append commits of bronze.clickstream (in cdc-run)")
    wait = sub.add_parser("wait-rows", help="wait until the newest snapshot holds N rows")
    wait.add_argument("--expect", type=int, required=True)
    wait.add_argument("--timeout", type=float, required=True)
    return parser


def _emit(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "commits":
            _emit(commits_report(snapshots()))
        else:
            if args.expect < 1 or args.timeout <= 0:
                print("--expect and --timeout must be positive", file=sys.stderr)
                return 2
            _emit(wait_rows(args.expect, args.timeout))
    except TimeoutError as exc:
        _emit({"timed_out": True, "detail": str(exc)})
        return 1
    except Exception as exc:
        print(f"error: {error_text(exc, [os.environ.get(CANARY_ENV, '')])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
