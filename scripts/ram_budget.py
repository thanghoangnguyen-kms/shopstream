"""Item 8's RAM budget: size the limits, run the load window and judge it by a tested rule.

Host side only: every Docker call is a fixed argv list built from `stack.compose_argv` (so FALL-03's
override seam reaches this driver unchanged), never a shell string. The pure parts (`size_limit`,
`proposed_limits`, `overlap`, `headroom`, `item8_verdict`, `window_record`, the argv builders) are
unit-tested; the live `window` is exercised by Plans 05-04 (a mini window) and 05-05 (the calibration
and verdict windows). Exit 0 on success (a verdict of go or fallback), 1 on an error, a timeout or an
inconclusive verdict, 2 on usage. The last line of stdout is one JSON line.

`window` runs, over one sampled interval: the 5 s sampler (`mem_report.py sample`), a `docker events`
capture, the clickstream generator, a loop of item 2's Spark MERGE job (each run a named container,
never `--rm`, so a kill leaves a record) and one `dbt_build_lk` DAG run. It waits for the sink to
drain, stops the sampler and writes window.json with every start and end time, so "all three were
active at one instant" is shown, not asserted.

`item8_verdict` reads `mem_report.py report --json-out` and window.json. It is `inconclusive`, never
go or fallback, for: an uncalibrated run (the calibration run is never judged, owner decision 2), a
report with no frames or fewer than `--min-frames`, a window where the required activities were not
all active at one instant, a Spark loop with no run that exited 0, a DAG run that did not succeed, a
service with no `mem_limit`, a long-running service that was never sampled, and a peak of 0 bytes.
Otherwise it is `fallback` (ADR-001's 1 broker, FALL-03) when the peak summed sample is over 10 GiB,
the limit total is over MemTotal minus 1 GiB, or any OOM kill, OOM event, exit 137, restart or stopped
long-running service appears, and `go` when none does. Both thresholds hold at equality: exactly
10 GiB and exactly the ceiling pass, one byte more fails.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Collection, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import airflow_check
import mem_report
from stack import compose_argv

REPO = Path(__file__).resolve().parents[1]
MIB = 1024**2
LIMIT_STEP = 32 * MIB
LIMIT_FACTOR = (5, 4)
DAG_ID = "dbt_build_lk"
SPARK_SERVICE = "spark-job"
GENERATOR_SERVICE = "cdc-run"
FALLBACK = "1 broker (FALL-03)"
ACTIVITIES = ("generator", "spark", "dag")
DBT_RUNS_SQL = (
    "SELECT coalesce(json_agg(row_to_json(r) ORDER BY r.id), '[]') FROM "
    "(SELECT id, run_id, state, start_date, end_date FROM dag_run "
    "WHERE dag_id = 'dbt_build_lk') r"
)
DAG_TERMINAL = ("success", "failed")
SPARK_NAME_PREFIX = f"shopstream-{SPARK_SERVICE}-run-"
DRAIN_TIMEOUT_S = 900.0
DAG_TIMEOUT_S = 900.0
SPARK_TIMEOUT_S = 900.0
STOP_TIMEOUT_S = 60.0
PROFILES_STREAMING_SPIKE = ("core", "streaming", "spike")
PROFILES_SPARK = ("core", "spike")
PROFILES_ORCHESTRATION = ("core", "streaming", "orchestration")
SPARK_RUN_NAME = re.compile(rf"{re.escape(SPARK_NAME_PREFIX)}[0-9a-f]{{12}}")
_UNBOUNDED = 2**62

Interval = tuple[int, int]


# --- pure rules ------------------------------------------------------------------------------


def size_limit(peak_bytes: int) -> int:
    """peak x 1.25 rounded up to a multiple of 32 MiB, in integers; at least one step."""
    if peak_bytes < 0:
        raise ValueError("a peak must not be negative")
    numerator, denominator = LIMIT_FACTOR
    needed = -(-peak_bytes * numerator // denominator)
    return max(-(-needed // LIMIT_STEP), 1) * LIMIT_STEP


def proposed_limits(
    peaks: Mapping[str, int], current: Mapping[str, int], capped: Collection[str]
) -> dict[str, dict[str, Any]]:
    """Per service in `current`: the sized limit, or the current one when never sampled or capped.

    A capped service is one whose memory use is page cache that the kernel reclaims (a Kafka
    broker, SeaweedFS): when the formula would raise its limit, the current limit stays (owner
    decision 2) and `capped` says so.
    """
    result: dict[str, dict[str, Any]] = {}
    for service in sorted(current):
        now = current[service]
        peak = peaks.get(service)
        if peak is None:
            result[service] = {
                "sampled": False,
                "peak_bytes": None,
                "current_bytes": now,
                "proposed_bytes": now,
                "capped": False,
            }
            continue
        sized = size_limit(peak)
        keep = service in capped and sized > now
        result[service] = {
            "sampled": True,
            "peak_bytes": peak,
            "current_bytes": now,
            "proposed_bytes": now if keep else sized,
            "capped": keep,
        }
    return result


def _merge(runs: Sequence[Interval]) -> list[Interval]:
    """Sorted, disjoint intervals; empty or backwards ones are dropped."""
    merged: list[Interval] = []
    for start, end in sorted(run for run in runs if run[0] < run[1]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _intersect(left: Sequence[Interval], right: Sequence[Interval]) -> list[Interval]:
    pieces: list[Interval] = []
    for a_start, a_end in left:
        for b_start, b_end in right:
            start, end = max(a_start, b_start), min(a_end, b_end)
            if start < end:
                pieces.append((start, end))
    return pieces


def overlap(
    generator: Interval | None,
    spark_runs: Sequence[Interval],
    dag: Interval | None,
    required: Collection[str],
) -> dict[str, Any]:
    """{all_active, overlap_ms, window}: the time the required activities were all active.

    Intervals are half-open in effect: two that only touch (one ends at the millisecond the other
    starts) share no instant. An activity that is not required is ignored even when it is missing;
    a required one with no interval means never all active. `window` is [first start, last end]
    of the shared time, `overlap_ms` its total length.
    """
    wanted = set(required)
    active: list[Interval] = [(-_UNBOUNDED, _UNBOUNDED)]
    if "generator" in wanted:
        active = _intersect(active, _merge([generator] if generator else []))
    if "spark" in wanted:
        active = _intersect(active, _merge(spark_runs))
    if "dag" in wanted:
        active = _intersect(active, _merge([dag] if dag else []))
    if not wanted & set(ACTIVITIES) or not active:
        return {"all_active": False, "overlap_ms": 0, "window": None}
    total = sum(end - start for start, end in active)
    window = [min(start for start, _ in active), max(end for _, end in active)]
    return {"all_active": total > 0, "overlap_ms": total, "window": window}


def headroom(peak_sum: int, limit_total: int, ceiling: int) -> dict[str, Any]:
    """What is left under the 10 GiB peak budget and under the limit ceiling, beside the reserve.

    The 384 MiB Week 5 reserve fits when both headrooms are at least that large.
    """
    peak_headroom = mem_report.PEAK_BUDGET_BYTES - peak_sum
    limit_headroom = ceiling - limit_total
    reserve = mem_report.W05_RESERVE_BYTES
    return {
        "peak_headroom_bytes": peak_headroom,
        "limit_headroom_bytes": limit_headroom,
        "w05_reserve_bytes": reserve,
        "w05_reserve_fits": peak_headroom >= reserve and limit_headroom >= reserve,
    }


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _span(value: Any) -> Interval | None:
    """(start_ms, end_ms) of a window entry, or None when either end is not an integer."""
    if not isinstance(value, Mapping):
        return None
    start, end = _int(value.get("start_ms")), _int(value.get("end_ms"))
    return (start, end) if start is not None and end is not None else None


def _rows(
    report: Mapping[str, Any], key: str, fields: Sequence[str]
) -> list[tuple[Any, ...]] | None:
    """The report's list of mappings as tuples of `fields`; None when it is not usable."""
    raw = report.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(row, Mapping) for row in raw):
        return None
    try:
        return [tuple(row[field] for field in fields) for row in raw]
    except KeyError:
        return None


def _figure_reasons(report: Mapping[str, Any], min_frames: int) -> list[str]:
    reasons: list[str] = []
    for key in ("frames", "peak_sum", "limit_total", "ceiling"):
        if _int(report.get(key)) is None:
            reasons.append(f"the report has no usable {key}")
    if not isinstance(report.get("states"), list):
        reasons.append("the report has no usable states")
    frames = _int(report.get("frames"))
    needed = max(min_frames, 1)
    if frames is not None and frames < needed:
        reasons.append(f"{frames} frames with container rows, fewer than {needed}")
    missing = report.get("missing_limits", [])
    if isinstance(missing, list):
        reasons.extend(f"service {name} has no mem_limit" for name in missing)
    return reasons


def _coverage_reasons(report: Mapping[str, Any]) -> list[str]:
    per_service = report.get("per_service", [])
    sampled = {
        row["service"]
        for row in per_service
        if isinstance(per_service, list) and isinstance(row, Mapping) and "service" in row
    }
    long_running = report.get("long_running", [])
    names = (
        {name for name in long_running if isinstance(name, str)}
        if isinstance(long_running, list)
        else set()
    )
    reasons = [f"service {name} was never sampled" for name in sorted(names - sampled)]
    if report.get("peak_sum") == 0:
        reasons.append("peak summed sample is 0 B; no memory use was observed")
    return reasons


def _activity_reasons(
    window: Mapping[str, Any], wanted: set[str]
) -> tuple[list[str], dict[str, Any]]:
    """Why the window cannot be judged, and the overlap of its required activities."""
    reasons: list[str] = []
    generator = _span(window.get("generator"))
    spark: list[Interval] = []
    runs = window.get("spark_runs")
    runs = runs if isinstance(runs, list) else []
    for run in runs:
        span = _span(run)
        if span is not None and isinstance(run, Mapping) and run.get("exit") == 0:
            spark.append(span)
    dag_run = window.get("dag_run")
    dag = _span(dag_run)
    if "spark" in wanted and not spark:
        reasons.append(f"no Spark run exited 0 ({len(runs)} runs in the window)")
    if "dag" in wanted:
        state = dag_run.get("state") if isinstance(dag_run, Mapping) else None
        if state != "success":
            reasons.append(f"the DAG run did not end in state success (state {state!r})")
    shared = overlap(generator, spark, dag, wanted)
    if not shared["all_active"]:
        names = ", ".join(name for name in ACTIVITIES if name in wanted)
        reasons.append(f"the required activities ({names}) were not all active at one instant")
    return reasons, shared


def item8_verdict(
    report: Mapping[str, Any],
    window: Mapping[str, Any],
    *,
    calibrated: bool,
    required: Collection[str] = ACTIVITIES,
    min_frames: int = 1,
) -> dict[str, Any]:
    """go, fallback or inconclusive for a mem_report JSON report and a window record."""
    wanted = set(required)
    reasons: list[str] = []
    if not calibrated:
        reasons.append(
            "the run is not calibrated: the calibration run is never judged (owner decision 2)"
        )
    reasons.extend(_figure_reasons(report, min_frames))
    reasons.extend(_coverage_reasons(report))
    activity, shared = _activity_reasons(window, wanted)
    reasons.extend(activity)
    peak_sum, limit_total, ceiling = (
        _int(report.get(k)) for k in ("peak_sum", "limit_total", "ceiling")
    )
    inspect = _rows(report, "inspect", ("service", "oom_killed", "restarts"))
    states = _rows(report, "states", ("service", "status", "exit_code"))
    events = _rows(report, "events", ("service", "action", "exit_code"))
    for key, rows in (("inspect", inspect), ("states", states), ("events", events)):
        if rows is None and f"the report has no usable {key}" not in reasons:
            reasons.append(f"the report has no usable {key}")
    result: dict[str, Any] = {
        "verdict": "inconclusive",
        "fallback": None,
        "reasons": reasons,
        "calibrated": calibrated,
        "overlap": shared,
        "headroom": None,
        "peak_sum": peak_sum,
        "limit_total": limit_total,
        "ceiling": ceiling,
    }
    if reasons or peak_sum is None or limit_total is None or ceiling is None:
        return result
    long_running = report.get("long_running", [])
    breaches = mem_report.evaluate(
        peak_sum,
        limit_total,
        [],
        ceiling + mem_report.VM_HEADROOM_BYTES,
        [(str(s), bool(o), int(r)) for s, o, r in inspect or []],
        states=[(str(s), str(st), int(c)) for s, st, c in states or []],
        long_running=[str(name) for name in long_running] if isinstance(long_running, list) else [],
        events=[(str(s), str(a), c if isinstance(c, int) else None) for s, a, c in events or []],
    )
    result["headroom"] = headroom(peak_sum, limit_total, ceiling)
    result["reasons"] = breaches
    if breaches:
        result["verdict"] = "fallback"
        result["fallback"] = FALLBACK
    else:
        result["verdict"] = "go"
    return result


def window_record(
    *,
    started_at_ms: int,
    ended_at_ms: int,
    generator: Mapping[str, Any],
    spark_runs: Sequence[Mapping[str, Any]],
    dag_run: Mapping[str, Any] | None,
    sampler: Mapping[str, Any],
    events_file: str,
    drain: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The window.json body: every start and end time, so "all active" is shown, not asserted."""
    return {
        "started_at_ms": started_at_ms,
        "ended_at_ms": ended_at_ms,
        "generator": dict(generator),
        "spark_runs": sorted((dict(run) for run in spark_runs), key=lambda r: r.get("start_ms", 0)),
        "dag_run": dict(dag_run) if dag_run is not None else None,
        "sampler": dict(sampler),
        "events_file": events_file,
        "drain": dict(drain) if drain is not None else None,
    }


def parse_dag_trigger(text: str) -> str:
    """The run id `airflow dags trigger -o json` printed on its last line."""
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        loaded = json.loads(lines[-1]) if lines else None
    except ValueError:
        loaded = None
    first = loaded[0] if isinstance(loaded, list) and loaded else loaded
    run_id = first.get("dag_run_id") if isinstance(first, Mapping) else None
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("no run id in the trigger output")
    return run_id


def dag_run_interval(row: Mapping[str, Any]) -> Interval | None:
    """(start, end) in epoch ms of a dag_run row, or None while either is missing."""
    try:
        return (
            airflow_check.parse_iso_ms(row.get("start_date")),
            airflow_check.parse_iso_ms(row.get("end_date")),
        )
    except ValueError:
        return None


# --- argv builders ---------------------------------------------------------------------------


def spark_name() -> str:
    """A fresh container name that `service_of` maps to spark-job."""
    return f"{SPARK_NAME_PREFIX}{secrets.token_hex(6)}"


def generator_argv(events: int, rate: float, procs: int, seed: int, knobs: bool) -> list[str]:
    """The clickstream generator in a cdc-run one-shot (removed on exit; its report is stdout).

    Every `run` here passes --no-deps: without it Compose starts the dependencies' one-shots again
    (lakekeeper-migrate ran before each job in the first mini window), a side effect no load
    window should have. The stack is up before a window starts.
    """
    argv = compose_argv(
        PROFILES_STREAMING_SPIKE,
        "run",
        "--no-deps",
        "-T",
        "--rm",
        GENERATOR_SERVICE,
        "python",
        "/app/clickstream_load.py",
        "run",
        "--events",
        str(events),
        "--rate",
        str(rate),
        "--procs",
        str(procs),
        "--seed",
        str(seed),
    )
    return [*argv, "--knobs"] if knobs else argv


def spark_argv(name: str) -> list[str]:
    """Item 2's MERGE job under a fixed name and without --rm, so a kill leaves a record."""
    if SPARK_RUN_NAME.fullmatch(name) is None:
        raise ValueError(f"not a {SPARK_NAME_PREFIX}<12 hex> container name")
    return compose_argv(
        PROFILES_SPARK,
        "run",
        "--no-deps",
        "-T",
        "--name",
        name,
        SPARK_SERVICE,
        "python",
        "/app/spark_v3_job.py",
    )


def dag_trigger_argv() -> list[str]:
    return compose_argv(
        PROFILES_ORCHESTRATION,
        "exec",
        "-T",
        "airflow-scheduler",
        "airflow",
        "dags",
        "trigger",
        DAG_ID,
        "-o",
        "json",
    )


def drain_argv(expect: int, timeout_s: float) -> list[str]:
    """Wait inside cdc-run until bronze.clickstream's newest snapshot holds `expect` rows."""
    return compose_argv(
        PROFILES_STREAMING_SPIKE,
        "run",
        "--no-deps",
        "-T",
        "--rm",
        GENERATOR_SERVICE,
        "python",
        "/app/throughput_check.py",
        "wait-rows",
        "--expect",
        str(expect),
        "--timeout",
        str(timeout_s),
    )


def sampler_argv(out: Path) -> list[str]:
    return [sys.executable, str(REPO / "scripts" / "mem_report.py"), "sample", "--out", str(out)]


def remove_argv(names: Sequence[str]) -> list[str]:
    """`docker rm` for Spark run containers only; any other name is refused."""
    for name in names:
        if SPARK_RUN_NAME.fullmatch(name) is None:
            raise ValueError(f"not a {SPARK_NAME_PREFIX}<12 hex> container name")
    return ["docker", "rm", *names]


# --- live window -----------------------------------------------------------------------------


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _last_json(path: Path) -> dict[str, Any] | None:
    """The last JSON object line of a capture, or None."""
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        loaded = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_dag(
    delay_s: float, origin: float, result: dict[str, Any], finished: threading.Event
) -> None:
    """Trigger the DAG after `delay_s`, then poll the airflow database until the run is terminal."""
    try:
        time.sleep(max(0.0, delay_s - (time.monotonic() - origin)))
        triggered = _now_ms()
        out = subprocess.run(
            dag_trigger_argv(), capture_output=True, text=True, check=True, timeout=120
        ).stdout
        run_id = parse_dag_trigger(out)
        result.update(run_id=run_id, state="triggered", start_ms=triggered, end_ms=None)
        deadline = time.monotonic() + DAG_TIMEOUT_S
        while True:
            rows = airflow_check.read_rows(DBT_RUNS_SQL)
            row = next((r for r in rows if r.get("run_id") == run_id), None)
            if row is not None and row.get("state") in DAG_TERMINAL:
                span = dag_run_interval(row)
                result.update(
                    state=row["state"],
                    start_ms=span[0] if span else triggered,
                    end_ms=span[1] if span else _now_ms(),
                )
                return
            if time.monotonic() > deadline:
                result.update(state="timeout", end_ms=_now_ms())
                return
            time.sleep(5)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.update(state="error", error=type(exc).__name__, end_ms=_now_ms())
    finally:
        finished.set()


def window(
    cap: Path,
    events: int,
    rate: float,
    procs: int,
    seed: int,
    knobs: bool,
    spark_delay: float,
    dag_delay: float,
    required: Collection[str],
) -> dict[str, Any]:
    """Run the load, the Spark loop and the DAG run under the sampler; write cap/window.json."""
    cap.mkdir(parents=True, exist_ok=True)
    wanted = set(required)
    samples, events_file = cap / "samples.jsonl", cap / "events.txt"
    started = _now_ms()
    origin = time.monotonic()
    spark_runs: list[dict[str, Any]] = []
    dag_result: dict[str, Any] = {}
    dag_finished = threading.Event()
    drain: dict[str, Any] | None = None
    with ExitStack() as stack:

        def log(name: str) -> Any:
            return stack.enter_context((cap / name).open("wb"))

        sampler = subprocess.Popen(
            sampler_argv(samples), stdout=log("sampler.out"), stderr=subprocess.STDOUT
        )
        stack.callback(_stop, sampler)
        watcher = subprocess.Popen(
            mem_report.events_argv(str(started // 1000)),
            stdout=stack.enter_context(events_file.open("wb")),
            stderr=log("events.err"),
        )
        stack.callback(_stop, watcher)
        generator_started = _now_ms()
        generator = subprocess.Popen(
            generator_argv(events, rate, procs, seed, knobs),
            stdout=log("gen.out"),
            stderr=log("gen.err"),
        )
        stack.callback(_stop, generator)
        if "dag" in wanted:
            threading.Thread(
                target=_run_dag,
                args=(dag_delay, origin, dag_result, dag_finished),
                daemon=True,
            ).start()

        def running() -> bool:
            dag_open = "dag" in wanted and not dag_finished.is_set()
            return generator.poll() is None or dag_open

        if "spark" in wanted:
            while running() and time.monotonic() - origin < spark_delay:
                time.sleep(0.5)
            while running():
                name = spark_name()
                begun = _now_ms()
                with (cap / f"spark-{len(spark_runs) + 1}.out").open("wb") as out:
                    try:
                        code = subprocess.run(
                            spark_argv(name),
                            stdout=out,
                            stderr=subprocess.STDOUT,
                            check=False,
                            timeout=SPARK_TIMEOUT_S,
                        ).returncode
                    except subprocess.TimeoutExpired:
                        code = -1
                spark_runs.append(
                    {"container": name, "start_ms": begun, "end_ms": _now_ms(), "exit": code}
                )
                time.sleep(1)
        while running():
            time.sleep(1)
        generator.wait()
        generator_ended = _now_ms()
        report = _last_json(cap / "gen.out") or {}
        knob_report = report.get("knobs")
        malformed = (
            knob_report.get("malformed", {}).get("count", 0) if isinstance(knob_report, dict) else 0
        )
        delivered = _int(report.get("delivered")) or 0
        if generator.returncode == 0 and delivered > malformed:
            expect = delivered - (malformed if isinstance(malformed, int) else 0)
            done = subprocess.run(
                drain_argv(expect, DRAIN_TIMEOUT_S),
                capture_output=True,
                text=True,
                check=False,
                timeout=DRAIN_TIMEOUT_S + 120,
            )
            parsed = None
            lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
            if lines:
                try:
                    parsed = json.loads(lines[-1])
                except ValueError:
                    parsed = None
            drain = {"exit": done.returncode, **(parsed if isinstance(parsed, dict) else {})}
        _stop(sampler)
        _stop(watcher)
    generator_record: dict[str, Any] = {
        "start_ms": _int(report.get("started_at_ms")) or generator_started,
        "end_ms": _int(report.get("ended_at_ms")) or generator_ended,
        "exit": generator.returncode,
        "events_requested": events,
        "delivered": delivered,
        "failed": report.get("failed"),
        "achieved_rate": report.get("achieved_rate"),
        "seed": seed,
        "procs": procs,
        "knobs": knobs,
    }
    record = window_record(
        started_at_ms=started,
        ended_at_ms=_now_ms(),
        generator=generator_record,
        spark_runs=spark_runs,
        dag_run=dag_result or None,
        sampler={"file": samples.name, "frames": len(mem_report.load_frames(samples))},
        events_file=events_file.name,
        drain=drain,
    )
    (cap / "window.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


# --- command line ----------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _read_object(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("the file does not hold a JSON object")
    return loaded


def _activities(text: str) -> set[str] | None:
    names = [name.strip() for name in text.split(",") if name.strip()]
    if not names or any(name not in ACTIVITIES for name in names):
        return None
    return set(names)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Item 8: the RAM budget window and its verdict.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("window", help="sample the load, the Spark loop and the DAG run")
    run.add_argument("--cap", type=Path, required=True, help="directory for the window's files")
    run.add_argument("--events", type=int, required=True)
    run.add_argument("--rate", type=float, default=20_000.0)
    run.add_argument("--procs", type=int, default=2)
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--knobs", action="store_true")
    run.add_argument("--spark-delay", type=float, default=10.0)
    run.add_argument("--dag-delay", type=float, default=10.0)
    run.add_argument("--no-spark", action="store_true", help="the Spark loop is not required")
    run.add_argument("--no-dag", action="store_true", help="the DAG run is not required")
    size = sub.add_parser("size", help="limits sized from the largest peak across the reports")
    size.add_argument("--report", type=Path, action="append", required=True)
    size.add_argument("--cap-services", default="", help="comma-separated page-cache services")
    judged = sub.add_parser("verdict", help="item 8's verdict from a report and a window")
    judged.add_argument("--report", type=Path, required=True)
    judged.add_argument("--window", type=Path, required=True)
    judged.add_argument("--calibrated", action="store_true")
    judged.add_argument("--required", default=",".join(ACTIVITIES))
    judged.add_argument("--min-frames", type=int, default=1)
    return parser


def _size_command(reports: Sequence[Path], cap_services: str) -> int:
    peaks: dict[str, int] = {}
    current: dict[str, int] = {}
    for path in reports:
        for row in _read_object(path).get("per_service", []):
            service = row["service"]
            peak, limit = _int(row.get("peak_bytes")), _int(row.get("limit_bytes"))
            if peak is not None:
                peaks[service] = max(peaks.get(service, 0), peak)
            if limit is not None:
                current[service] = max(current.get(service, 0), limit)
    capped = sorted(name.strip() for name in cap_services.split(",") if name.strip())
    _emit({"proposed": proposed_limits(peaks, current, capped), "cap_services": capped})
    return 0


def _verdict_command(args: argparse.Namespace, wanted: set[str]) -> int:
    try:
        report, window_body = _read_object(args.report), _read_object(args.window)
    except (OSError, ValueError) as exc:
        reason = f"the input files cannot be read: {type(exc).__name__}"
        _emit({"verdict": "inconclusive", "fallback": None, "reasons": [reason]})
        return 1
    result = item8_verdict(
        report,
        window_body,
        calibrated=args.calibrated,
        required=wanted,
        min_frames=args.min_frames,
    )
    _emit(result)
    return 1 if result["verdict"] == "inconclusive" else 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "size":
            return _size_command(args.report, args.cap_services)
        if args.command == "verdict":
            wanted = _activities(args.required)
            if wanted is None:
                print(f"--required must name some of {', '.join(ACTIVITIES)}", file=sys.stderr)
                return 2
            return _verdict_command(args, wanted)
        required = {name for name in ACTIVITIES if name != "spark" or not args.no_spark}
        if args.no_dag:
            required.discard("dag")
        record = window(
            args.cap,
            args.events,
            args.rate,
            args.procs,
            args.seed,
            args.knobs,
            args.spark_delay,
            args.dag_delay,
            required,
        )
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    _emit(record)
    drain = record["drain"]
    return (
        0
        if record["generator"]["exit"] == 0 and drain is not None and drain.get("exit") == 0
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
