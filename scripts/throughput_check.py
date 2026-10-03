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
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import cdc_check
import connect_admin
from read_v3 import error_text, identifier
from spark_v3_job import WAREHOUSE, catalog_prefix, http_request, lakekeeper_url

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


# --- the set check: interval arithmetic over half-open runs --------------------------------------

Runs = list[list[int]]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_runs(runs: Iterable[Sequence[Any]], what: str = "run") -> Runs:
    """Sorted half-open runs with touching and overlapping ones merged; ValueError for a bad run."""
    cleaned: Runs = []
    for run in runs:
        if len(run) != 2 or not _is_int(run[0]) or not _is_int(run[1]):
            raise ValueError(f"not a valid {what}")
        start, stop = int(run[0]), int(run[1])
        if start < 0 or stop <= start:
            raise ValueError(f"not a valid {what}")
        cleaned.append([start, stop])
    cleaned.sort()
    merged: Runs = []
    for start, stop in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    return merged


def _by_partition(runs_by_partition: Mapping[Any, Sequence[Sequence[Any]]]) -> dict[int, Runs]:
    return {int(p): _clean_runs(runs) for p, runs in runs_by_partition.items()}


def _subtract(a: Runs, b: Runs) -> Runs:
    """The runs of `a` (sorted, merged) that `b` (sorted, merged) does not cover."""
    out: Runs = []
    first = 0
    for start, stop in a:
        cursor = start
        while first < len(b) and b[first][1] <= cursor:
            first += 1
        k = first
        while k < len(b) and b[k][0] < stop:
            if b[k][0] > cursor:
                out.append([cursor, b[k][0]])
            cursor = max(cursor, b[k][1])
            if cursor >= stop:
                break
            k += 1
        if cursor < stop:
            out.append([cursor, stop])
    return out


def _size(runs: Runs) -> int:
    return sum(stop - start for start, stop in runs)


def compare_runs(
    expected: Mapping[Any, Sequence[Sequence[Any]]],
    bronze: Mapping[Any, Sequence[Sequence[Any]]],
    allowed_missing: Mapping[Any, Sequence[Sequence[Any]]],
) -> dict[str, Any]:
    """Per-partition comparison of the generator's delivered runs with bronze's offset islands.

    `missing` counts expected offsets bronze lacks and that are not in `allowed_missing`;
    `missing_allowed` counts the ones that are (a malformed record never reaches bronze); `extra`
    counts bronze offsets nobody delivered, a partition only bronze holds included. `equal` is
    missing 0 and extra 0. Samples are [partition, start, stop] pieces, sorted and cut to 20. The
    arithmetic is on runs, so memory is O(runs) however many records there are.
    """
    want = _by_partition(expected)
    have = _by_partition(bronze)
    allowed = _by_partition(allowed_missing)
    missing = extra = forgiven_total = 0
    missing_pieces: list[list[int]] = []
    extra_pieces: list[list[int]] = []
    for partition in sorted(set(want) | set(have)):
        wanted, held = want.get(partition, []), have.get(partition, [])
        lost = _subtract(wanted, held)
        hard = _subtract(lost, allowed.get(partition, []))
        forgiven_total += _size(lost) - _size(hard)
        missing += _size(hard)
        missing_pieces += [[partition, start, stop] for start, stop in hard]
        stray = _subtract(held, wanted)
        extra += _size(stray)
        extra_pieces += [[partition, start, stop] for start, stop in stray]
    return {
        "equal": missing == 0 and extra == 0,
        "missing": missing,
        "extra": extra,
        "missing_allowed": forgiven_total,
        "missing_sample": sorted(missing_pieces)[: cdc_check.SAMPLE_LIMIT],
        "extra_sample": sorted(extra_pieces)[: cdc_check.SAMPLE_LIMIT],
    }


def islands_from_rows(rows: Iterable[Sequence[Any]]) -> dict[int, Runs]:
    """(partition, start, stop) rows from the islands query as {partition: sorted merged runs}."""
    grouped: dict[int, Runs] = {}
    for row in rows:
        if len(row) != 3 or not _is_int(row[0]) or int(row[0]) < 0:
            raise ValueError("not a valid island")
        grouped.setdefault(int(row[0]), []).append([row[1], row[2]])
    return {p: _clean_runs(runs, "island") for p, runs in sorted(grouped.items())}


# --- disk: kafka-log-dirs, df, colima list, the 50M projection ----------------------------------


def parse_log_dirs(text: str, topic: str = TOPIC) -> dict[str, int]:
    """{bytes, replicas, brokers} from kafka-log-dirs.sh's output, for `topic`'s partitions only.

    The tool prints two status lines and one JSON line. Every broker's replica of every partition is
    summed once, so the figure already holds all replicas and must never be multiplied again. A
    future replica (a move in progress) is skipped.
    """
    body: Mapping[str, Any] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("{"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("brokers"), list):
                body = parsed
                break
    if body is None:
        raise ValueError("no kafka-log-dirs JSON line found")
    pattern = re.compile(re.escape(topic) + r"-\d+")
    total = replicas = 0
    brokers: set[int] = set()
    for broker in body["brokers"]:
        for log_dir in broker.get("logDirs", []):
            for part in log_dir.get("partitions", []):
                if not pattern.fullmatch(str(part.get("partition", ""))) or part.get("isFuture"):
                    continue
                total += int(part["size"])
                replicas += 1
                brokers.add(int(broker["broker"]))
    return {"bytes": total, "replicas": replicas, "brokers": len(brokers)}


def parse_df(text: str) -> dict[str, int]:
    """{size, used, avail} in bytes from `df -B1 --output=size,used,avail`: the first row of 3 integers."""
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) == 3 and all(token.isdigit() for token in tokens):
            return {"size": int(tokens[0]), "used": int(tokens[1]), "avail": int(tokens[2])}
    raise ValueError("no df figures found")


def parse_colima_list(text: str, profile: str = "default") -> dict[str, int]:
    """{disk_bytes, memory_bytes, cpus} of one profile from `colima list --json`.

    The command prints one JSON object per line (or an array) with `name`, `cpus`, `memory` and
    `disk`, the last two in bytes, e.g. {"name":"default","cpus":4,"memory":12884901888,"disk":42949672960}.
    """
    stripped = text.strip()
    entries: list[Any] = []
    if stripped.startswith("["):
        loaded = json.loads(stripped)
        entries = loaded if isinstance(loaded, list) else []
    else:
        entries = [json.loads(line) for line in stripped.splitlines() if line.strip()]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == profile:
            try:
                return {
                    "disk_bytes": int(entry["disk"]),
                    "memory_bytes": int(entry["memory"]),
                    "cpus": int(entry["cpus"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("colima list entry lacks disk, memory or cpus") from exc
    raise ValueError(f"colima list has no profile {profile}")


def disk_projection(
    kafka_bytes: int, kafka_records: int, bronze_bytes: int, bronze_rows: int
) -> int:
    """50,000,000 x (kafka_bytes / kafka_records + bronze_bytes / bronze_rows), rounded up, in integers.

    `kafka_bytes` is kafka-log-dirs' sum over every broker (all replicas already), so nothing is
    multiplied by the replication factor here.
    """
    if kafka_records <= 0:
        raise ValueError("kafka_records must be positive")
    if bronze_rows <= 0:
        raise ValueError("bronze_rows must be positive")
    numerator = TARGET_EVENTS * (kafka_bytes * bronze_rows + bronze_bytes * kafka_records)
    return -(-numerator // (kafka_records * bronze_rows))


def within_disk(projected: int, disk_bytes: int) -> bool:
    """True when `projected` is at most 3/4 of the disk, decided in integers (exactly 75% passes)."""
    return projected * DISK_SHARE[1] <= disk_bytes * DISK_SHARE[0]


# --- item 12's verdict --------------------------------------------------------------------------


def _verdict_result(verdict: str, reasons: list[str], figures: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "fallback": FALLBACK if verdict == "fallback" else None,
        "reasons": reasons,
        "figures": figures,
    }


def _int_or_none(value: Any) -> int | None:
    return int(value) if _is_int(value) else None


def _positive(value: Any) -> int | None:
    number = _int_or_none(value)
    return number if number is not None and number > 0 else None


def _percent(projected: int, disk_bytes: int) -> float:
    return float(round(Fraction(projected * 100, disk_bytes), 1))


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def item12_verdict(
    analysis: Mapping[str, Any],
    generator: Mapping[str, Any],
    logdirs: Mapping[str, Any],
    disk: Mapping[str, Any],
    primary: str | None = None,
) -> dict[str, Any]:
    """go, fallback or inconclusive for item 12 (ADR-001 go criterion 12, ROADMAP SC2).

    inconclusive (the harness failed, never go or fallback) for: no commit, one commit or a zero span;
    a missing or empty generator report, zero delivered events or fewer than 5,000,000; a
    generator-bound run (the generator's rate and the committed throughput both under 14,000
    events/s); a generator whose counts disagree with Kafka's offsets; or a missing island, log-dirs,
    bronze-file or disk input. Otherwise go only when commits >= 5, committed events >= 5,000,000,
    committed throughput >= 14,000 events/s, bronze's (partition, offset) islands equal the
    generator's delivered runs minus its malformed offsets, no (partition, offset) is held twice,
    event duplicates equal the generator's intended count, malformed offsets equal the DLQ's record
    count, and the 50M projection is within 75 percent of the primary disk; else fallback names each
    failed criterion. `primary` (colima or df) defaults to the disk figure's own.
    """
    figures: dict[str, Any] = {}
    unusable: list[str] = []

    raw = analysis.get("commits")
    commits = sorted(
        (c for c in raw if isinstance(c, Mapping) and _added_records(c) > 0)
        if isinstance(raw, list)
        else [],
        key=lambda c: (
            int(c.get("sequence_number", 0)),
            int(c.get("committed_at_ms", 0)),
            int(c.get("snapshot_id", 0)),
        ),
    )
    flow = committed_throughput(commits)
    committed_events = sum(_added_records(c) for c in commits)
    figures.update(
        commits=len(commits),
        committed_events=committed_events,
        rows_2_to_n=flow["rows_2_to_n"],
        span_ms=flow["span_ms"],
        events_per_s=flow["events_per_s"],
    )
    if len(commits) == 0:
        unusable.append("no commit reached bronze")
    elif len(commits) == 1:
        unusable.append("only one commit reached bronze, so no throughput exists")
    elif not flow["computable"]:
        unusable.append("the first and the last commit share one time, so the span is zero")

    per_partition = generator.get("per_partition")
    knobs = generator.get("knobs")
    knobs = knobs if isinstance(knobs, Mapping) else {}
    delivered = _positive(generator.get("delivered"))
    if not isinstance(per_partition, Mapping) or not per_partition:
        unusable.append("the generator report is missing or empty")
    elif delivered is None:
        unusable.append("the generator delivered zero events")
    elif delivered < MIN_EVENTS:
        unusable.append(f"fewer than {MIN_EVENTS:,} delivered events ({delivered:,})")
    figures["delivered"] = delivered

    gen_below_floor = False
    started, ended = (
        _int_or_none(generator.get("started_at_ms")),
        _int_or_none(generator.get("ended_at_ms")),
    )
    if delivered is not None and started is not None and ended is not None and ended > started:
        span = ended - started
        figures["generator_events_per_s"] = float(round(Fraction(delivered * 1000, span), 1))
        gen_below_floor = delivered * 1000 < FLOOR_EVENTS_PER_S * span
    elif _is_int(generator.get("achieved_rate")) or isinstance(
        generator.get("achieved_rate"), float
    ):
        figures["generator_events_per_s"] = generator["achieved_rate"]
        gen_below_floor = generator["achieved_rate"] < FLOOR_EVENTS_PER_S
    if gen_below_floor and flow["computable"] and not flow["meets_floor"]:
        unusable.append(
            "generator-bound run: the generator's rate and the committed throughput are both "
            f"under {FLOOR_EVENTS_PER_S:,} events/s"
        )
    if (
        isinstance(per_partition, Mapping)
        and per_partition
        and generator.get("offsets_consistent") is not True
    ):
        unusable.append("the generator's per-partition counts disagree with Kafka's offsets")

    # The set check: bronze's islands against the generator's delivered runs.
    islands = analysis.get("islands")
    malformed = knobs.get("malformed")
    malformed = malformed if isinstance(malformed, Mapping) else {}
    set_check: dict[str, Any] | None = None
    malformed_runs: Mapping[Any, Any] = malformed.get("runs") or {}
    if isinstance(per_partition, Mapping) and per_partition and isinstance(islands, Mapping):
        try:
            set_check = compare_runs(
                {p: facts["runs"] for p, facts in per_partition.items()},
                islands,
                malformed_runs,
            )
        except (KeyError, TypeError, ValueError):
            unusable.append("the generator's runs or bronze's islands cannot be read")
    elif isinstance(per_partition, Mapping) and per_partition:
        unusable.append("bronze's offset islands are missing")
    figures["set_check"] = set_check
    try:
        malformed_count = sum(_size(runs) for runs in _by_partition(malformed_runs).values())
    except (TypeError, ValueError):
        malformed_count = 0
    figures["malformed_allowed_missing"] = malformed_count

    kafka = analysis.get("kafka")
    kafka = kafka if isinstance(kafka, Mapping) else {}
    ends = kafka.get("partitions")
    if isinstance(ends, Mapping) and isinstance(per_partition, Mapping):
        for partition, facts in ends.items():
            ours = per_partition.get(str(partition))
            if isinstance(ours, Mapping) and ours.get("end") != facts.get("end"):
                unusable.append("Kafka's end offsets differ from the generator's")
                break
    dlq_records = kafka.get("dlq_records", 0)
    figures["dlq_records"] = dlq_records

    rows = _int_or_none(analysis.get("rows"))
    distinct_offsets = _int_or_none(analysis.get("distinct_offsets"))
    distinct_events = _int_or_none(analysis.get("distinct_event_ids"))
    sink_duplicates: int | None = None
    event_duplicates: int | None = None
    if rows is None or distinct_offsets is None or distinct_events is None:
        unusable.append("bronze's row and distinct counts are missing")
    else:
        sink_duplicates = rows - distinct_offsets
        event_duplicates = rows - distinct_events
    intended = knobs.get("intended_duplicates", 0)
    figures.update(
        sink_duplicates=sink_duplicates,
        event_duplicates_observed=event_duplicates,
        event_duplicates_intended=intended,
    )

    # Disk: Kafka bytes once over every replica, bronze data files, two bases, one primary.
    log_bytes, log_replicas = _positive(logdirs.get("bytes")), _positive(logdirs.get("replicas"))
    brokers = _positive(logdirs.get("brokers"))
    bronze = analysis.get("bronze")
    bronze = bronze if isinstance(bronze, Mapping) else {}
    bronze_bytes, bronze_rows = _positive(bronze.get("bytes")), _positive(bronze.get("rows"))
    colima_bytes, df_bytes = (
        _positive(disk.get("colima_disk_bytes")),
        _positive(disk.get("df_size_bytes")),
    )
    basis = primary if primary is not None else disk.get("primary")
    if log_bytes is None or log_replicas is None or brokers is None:
        unusable.append("the kafka-log-dirs figure is missing")
    elif isinstance(per_partition, Mapping) and log_replicas != len(per_partition) * brokers:
        unusable.append(
            f"kafka-log-dirs counts {log_replicas} replicas, expected {len(per_partition)} "
            f"partitions on {brokers} brokers"
        )
    if bronze_bytes is None or bronze_rows is None:
        unusable.append("bronze's data-file bytes and rows are missing")
    if colima_bytes is None or df_bytes is None or basis not in {"colima", "df"}:
        unusable.append("the disk figures or the primary disk basis are missing")
    projected: int | None = None
    if (
        delivered is not None
        and log_bytes is not None
        and bronze_bytes is not None
        and bronze_rows is not None
    ):
        projected = disk_projection(log_bytes, delivered, bronze_bytes, bronze_rows)
        figures.update(
            kafka_bytes=log_bytes,
            bronze_bytes=bronze_bytes,
            kafka_bytes_per_million=_ceil_div(log_bytes * 1_000_000, delivered),
            bronze_bytes_per_million=_ceil_div(bronze_bytes * 1_000_000, bronze_rows),
            projected_bytes=projected,
        )
    disk_report: dict[str, Any] = {"primary": basis}
    if projected is not None and colima_bytes is not None and df_bytes is not None:
        for name, size in (("colima", colima_bytes), ("df", df_bytes)):
            disk_report[name] = {
                "disk_bytes": size,
                "threshold_bytes": size * DISK_SHARE[0] // DISK_SHARE[1],
                "percent": _percent(projected, size),
                "within": within_disk(projected, size),
            }
    figures["disk"] = disk_report

    if unusable:
        return _verdict_result("inconclusive", unusable, figures)

    reasons: list[str] = []
    if len(commits) < MIN_COMMITS:
        reasons.append(f"commits: {len(commits)} append commits, at least {MIN_COMMITS} are needed")
    if committed_events < MIN_EVENTS:
        reasons.append(f"committed events: {committed_events:,} is below {MIN_EVENTS:,}")
    if not flow["meets_floor"]:
        reasons.append(
            f"throughput: {flow['events_per_s']} events/s over commits 2..N is below "
            f"{FLOOR_EVENTS_PER_S:,}"
        )
    if set_check is not None and not set_check["equal"]:
        reasons.append(
            f"set check: {set_check['missing']} offsets missing and {set_check['extra']} extra "
            "against the generator's delivered runs"
        )
    if sink_duplicates != 0:
        reasons.append(f"sink duplicates: {sink_duplicates} rows repeat a (partition, offset)")
    if event_duplicates != intended:
        reasons.append(
            f"event duplicates: {event_duplicates} observed, {intended} intended by the generator"
        )
    if malformed_count != dlq_records:
        reasons.append(
            f"malformed: {malformed_count} offsets allowed missing, {dlq_records} records in the DLQ"
        )
    chosen = disk_report[str(basis)]
    if not chosen["within"]:
        reasons.append(
            f"disk: the {TARGET_EVENTS:,}-event projection is {chosen['percent']} percent of the "
            f"{basis} disk, over the 75 percent line"
        )
    return _verdict_result("fallback" if reasons else "go", reasons, figures)


# --- reset: the clickstream objects only (constants, no run-time input) -------------------------

RESET_CONNECTOR = "clickstream-sink"
RESET_TABLE = "clickstream"
RESET_TOPICS = ("clickstream", "clickstream.dlq", "control-iceberg-clicks")
GROUP_MARKER = "clickstream-sink"
TOPIC_GONE_TIMEOUT_S = 120.0
GROUP_GONE_TIMEOUT_S = 60.0


def groups_to_delete(group_ids: Iterable[str]) -> list[str]:
    """The consumer groups that belong to the clickstream sink (its name in the id), sorted."""
    return sorted(group for group in group_ids if GROUP_MARKER in group)


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


# --- live (cdc-run): offset islands, data files, Kafka offsets, reset -----------------------------

DLQ_TOPIC = "clickstream.dlq"
PARTITION_COLUMN = cdc_check.KAFKA_META_COLUMNS[1]
OFFSET_COLUMN = cdc_check.KAFKA_META_COLUMNS[2]
EVENT_ID_COLUMN = "event_id"
SOURCE = f"lk.{identifier(cdc_check.NAMESPACE)}.{identifier(TABLE)}"
KAFKA_TIMEOUT_S = 30


def islands() -> dict[str, Any]:
    """Bronze's (partition, offset) islands and counts, read through DuckDB in the spike image.

    Per partition one aggregate gives min, max, rows and distinct offsets; a partition whose
    distinct offsets fill min..max is one island with no window at all, and only a partition with a
    hole runs the gaps-and-islands query (dense_rank, so a repeated offset stays in its island).
    """
    from read_v3 import duckdb_connect

    con = duckdb_connect()
    con.execute(
        f"ATTACH '{WAREHOUSE}' AS lk (TYPE iceberg, "
        f"ENDPOINT '{lakekeeper_url()}/catalog', AUTHORIZATION_TYPE 'none')"
    )
    con.execute("SET memory_limit='1GB'")
    con.execute("SET preserve_insertion_order=false")
    summary = con.execute(
        f"SELECT {PARTITION_COLUMN}, min({OFFSET_COLUMN}), max({OFFSET_COLUMN}), count(*), "
        f"count(DISTINCT {OFFSET_COLUMN}) FROM {SOURCE} GROUP BY {PARTITION_COLUMN} "
        f"ORDER BY {PARTITION_COLUMN}"
    ).fetchall()
    rows: list[tuple[int, int, int]] = []
    total = distinct = 0
    for partition, low, high, count, distinct_count in summary:
        total += int(count)
        distinct += int(distinct_count)
        if int(distinct_count) == int(high) - int(low) + 1:
            rows.append((int(partition), int(low), int(high) + 1))
            continue
        pieces = con.execute(
            f"SELECT min({OFFSET_COLUMN}), max({OFFSET_COLUMN}) + 1 FROM "
            f"(SELECT {OFFSET_COLUMN}, {OFFSET_COLUMN} - dense_rank() OVER "
            f"(ORDER BY {OFFSET_COLUMN}) AS grp FROM {SOURCE} WHERE {PARTITION_COLUMN} = ?) "
            "GROUP BY grp ORDER BY 1",
            [int(partition)],
        ).fetchall()
        rows += [(int(partition), int(start), int(stop)) for start, stop in pieces]
    events = con.execute(f"SELECT count(DISTINCT {EVENT_ID_COLUMN}) FROM {SOURCE}").fetchone()
    return {
        "islands": {str(p): runs for p, runs in islands_from_rows(rows).items()},
        "rows": total,
        "distinct_offsets": distinct,
        "distinct_event_ids": int(events[0]) if events else 0,
    }


def bronze_files() -> dict[str, int]:
    """Data-file bytes, records and file count of bronze.clickstream's current snapshot."""
    total = rows = files = 0
    for task in cdc_check.load_bronze(TABLE).scan().plan_files():
        total += int(task.file.file_size_in_bytes)
        rows += int(task.file.record_count)
        files += 1
    return {"bytes": total, "rows": rows, "files": files}


def kafka_offsets() -> dict[str, Any]:
    """Earliest and latest offset of each clickstream partition, and the DLQ's record count."""
    from confluent_kafka import TopicPartition
    from confluent_kafka.admin import AdminClient, OffsetSpec

    admin = AdminClient({"bootstrap.servers": cdc_check.BOOTSTRAP})

    def partitions(topic: str) -> list[Any]:
        meta = admin.list_topics(topic, timeout=KAFKA_TIMEOUT_S).topics[topic]
        return [TopicPartition(topic, p) for p in sorted(meta.partitions)]

    main_parts, dlq_parts = partitions(TOPIC), partitions(DLQ_TOPIC)

    def offsets(spec: Any) -> dict[tuple[str, int], int]:
        futures = admin.list_offsets(
            {tp: spec for tp in [*main_parts, *dlq_parts]}, request_timeout=KAFKA_TIMEOUT_S
        )
        return {(tp.topic, tp.partition): int(f.result().offset) for tp, f in futures.items()}

    earliest, latest = offsets(OffsetSpec.earliest()), offsets(OffsetSpec.latest())
    return {
        "partitions": {
            str(tp.partition): {
                "start": earliest[(TOPIC, tp.partition)],
                "end": latest[(TOPIC, tp.partition)],
            }
            for tp in main_parts
        },
        "dlq_records": sum(
            latest[(DLQ_TOPIC, tp.partition)] - earliest[(DLQ_TOPIC, tp.partition)]
            for tp in dlq_parts
        ),
    }


def analyze() -> dict[str, Any]:
    """Everything the verdict reads from the live stack, as one JSON object."""
    found = snapshots()
    return {
        **commits_report(found),
        "head_total_records": newest_total_records(found),
        **islands(),
        "kafka": kafka_offsets(),
        "bronze": bronze_files(),
    }


def _wait_until(done: Any, timeout_s: float, poll_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if done():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def reset() -> dict[str, Any]:
    """Remove the clickstream sink, its three topics, its consumer groups and bronze.clickstream.

    Constants only: the connector name, RESET_TOPICS and RESET_TABLE, with no argument, so no other
    topic, group or table can be reached. The table goes through Lakekeeper's REST purge (204 or 404).
    """
    from confluent_kafka import KafkaException
    from confluent_kafka.admin import AdminClient

    connect_admin.delete(RESET_CONNECTOR)
    admin = AdminClient({"bootstrap.servers": cdc_check.BOOTSTRAP})
    present = set(admin.list_topics(timeout=KAFKA_TIMEOUT_S).topics)
    doomed = [topic for topic in RESET_TOPICS if topic in present]
    if doomed:
        for future in admin.delete_topics(doomed, operation_timeout=KAFKA_TIMEOUT_S).values():
            future.result()

        def topics_gone() -> bool:
            listed = set(admin.list_topics(timeout=KAFKA_TIMEOUT_S).topics)
            return not listed.intersection(RESET_TOPICS)

        if not _wait_until(topics_gone, TOPIC_GONE_TIMEOUT_S):
            raise TimeoutError("the clickstream topics were still listed after the delete")
    listed_groups = admin.list_consumer_groups().result().valid
    groups = groups_to_delete(group.group_id for group in listed_groups)
    pending = list(groups)

    def groups_deleted() -> bool:
        if not pending:
            return True
        futures = admin.delete_consumer_groups(list(pending))
        for name, future in futures.items():
            try:
                future.result()
            except KafkaException:
                continue
            pending.remove(name)
        return not pending

    if not _wait_until(groups_deleted, GROUP_GONE_TIMEOUT_S):
        raise TimeoutError(f"{len(pending)} clickstream-sink consumer groups were still not empty")
    url = (
        f"{lakekeeper_url()}/catalog/v1/{catalog_prefix()}/namespaces/"
        f"{identifier(cdc_check.NAMESPACE)}/tables/{identifier(RESET_TABLE)}?purgeRequested=true"
    )
    status, _ = http_request("DELETE", url, {}, None)
    if status not in {204, 404}:
        raise RuntimeError(f"purging bronze.{RESET_TABLE} returned HTTP {status}")
    return {
        "connector": RESET_CONNECTOR,
        "topics": doomed,
        "groups": groups,
        "table": f"bronze.{RESET_TABLE}",
        "table_status": status,
    }


# --- CLI ----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Item 12: commits, throughput and the set check.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("commits", help="the append commits of bronze.clickstream (in cdc-run)")
    wait = sub.add_parser("wait-rows", help="wait until the newest snapshot holds N rows")
    wait.add_argument("--expect", type=int, required=True)
    wait.add_argument("--timeout", type=float, required=True)
    sub.add_parser("analyze", help="commits, islands, counts, offsets and files (in cdc-run)")
    sub.add_parser(
        "reset", help="remove the clickstream sink, topics, groups and table (in cdc-run)"
    )
    sub.add_parser("logdirs", help="parse kafka-log-dirs output from stdin (host)")
    figures = sub.add_parser("disk", help="the colima and df disk figures with the primary (host)")
    figures.add_argument("--colima-list", type=Path, required=True)
    figures.add_argument("--df", type=Path, required=True)
    figures.add_argument("--primary", choices=("colima", "df"), required=True)
    judged = sub.add_parser("verdict", help="item 12's verdict from the captured files (host)")
    judged.add_argument("--analysis", type=Path, required=True)
    judged.add_argument("--generator", type=Path, required=True)
    judged.add_argument("--logdirs", type=Path, required=True)
    judged.add_argument("--disk", type=Path, required=True)
    return parser


def _emit(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _read_json(path: Path) -> dict[str, Any]:
    """A capture's JSON object: the whole file, else its last non-blank line."""
    text = path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(text)
    except ValueError:
        lines = [line for line in text.splitlines() if line.strip()]
        loaded = json.loads(lines[-1]) if lines else None
    if not isinstance(loaded, dict):
        raise ValueError("the file does not hold a JSON object")
    return loaded


def _disk_command(colima_path: Path, df_path: Path, primary: str) -> dict[str, Any]:
    colima = parse_colima_list(colima_path.read_text(encoding="utf-8"))
    df = parse_df(df_path.read_text(encoding="utf-8"))
    return {
        "colima_disk_bytes": colima["disk_bytes"],
        "colima_memory_bytes": colima["memory_bytes"],
        "colima_cpus": colima["cpus"],
        "df_size_bytes": df["size"],
        "df_used_bytes": df["used"],
        "df_avail_bytes": df["avail"],
        "primary": primary,
    }


def _verdict_command(args: argparse.Namespace) -> int:
    try:
        analysis = _read_json(args.analysis)
        generator = _read_json(args.generator)
        disk = _read_json(args.disk)
        text = args.logdirs.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        reason = f"the input files cannot be read: {type(exc).__name__}"
        _emit(_verdict_result("inconclusive", [reason], {}))
        return 1
    try:
        logdirs: dict[str, Any] = dict(parse_log_dirs(text, TOPIC))
    except ValueError:
        logdirs = {}
    result = item12_verdict(analysis, generator, logdirs, disk)
    _emit(result)
    return 1 if result["verdict"] == "inconclusive" else 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "commits":
            _emit(commits_report(snapshots()))
        elif args.command == "wait-rows":
            if args.expect < 1 or args.timeout <= 0:
                print("--expect and --timeout must be positive", file=sys.stderr)
                return 2
            _emit(wait_rows(args.expect, args.timeout))
        elif args.command == "analyze":
            _emit(analyze())
        elif args.command == "reset":
            _emit(reset())
        elif args.command == "logdirs":
            _emit(parse_log_dirs(sys.stdin.read(), TOPIC))
        elif args.command == "disk":
            _emit(_disk_command(args.colima_list, args.df, args.primary))
        else:
            return _verdict_command(args)
    except TimeoutError as exc:
        _emit({"timed_out": True, "detail": str(exc)})
        return 1
    except Exception as exc:
        print(f"error: {error_text(exc, [os.environ.get(CANARY_ENV, '')])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
