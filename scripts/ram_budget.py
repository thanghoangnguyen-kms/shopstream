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
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Collection, Mapping, Sequence
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

Interval = tuple[int, int]


# --- pure rules ------------------------------------------------------------------------------


def size_limit(peak_bytes: int) -> int:
    """peak x 1.25 rounded up to a multiple of 32 MiB, in integers; at least one step."""
    raise NotImplementedError("size_limit is not written yet")


def proposed_limits(
    peaks: Mapping[str, int], current: Mapping[str, int], capped: Collection[str]
) -> dict[str, dict[str, Any]]:
    """Per service in `current`: the sized limit, or the current one when never sampled or capped."""
    raise NotImplementedError("proposed_limits is not written yet")


def overlap(
    generator: Interval | None,
    spark_runs: Sequence[Interval],
    dag: Interval | None,
    required: Collection[str],
) -> dict[str, Any]:
    """{all_active, overlap_ms, window}: the time the required activities were all active."""
    raise NotImplementedError("overlap is not written yet")


def headroom(peak_sum: int, limit_total: int, ceiling: int) -> dict[str, Any]:
    """What is left under the 10 GiB peak budget and under the limit ceiling, beside the reserve."""
    raise NotImplementedError("headroom is not written yet")


def item8_verdict(
    report: Mapping[str, Any],
    window: Mapping[str, Any],
    *,
    calibrated: bool,
    required: Collection[str] = ACTIVITIES,
    min_frames: int = 1,
) -> dict[str, Any]:
    """go, fallback or inconclusive for a mem_report JSON report and a window record."""
    raise NotImplementedError("item8_verdict is not written yet")


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
    """The window.json body."""
    raise NotImplementedError("window_record is not written yet")


def parse_dag_trigger(text: str) -> str:
    """The run id `airflow dags trigger -o json` printed on its last line."""
    raise NotImplementedError("parse_dag_trigger is not written yet")


def dag_run_interval(row: Mapping[str, Any]) -> Interval | None:
    """(start, end) in epoch ms of a dag_run row, or None while either is missing."""
    raise NotImplementedError("dag_run_interval is not written yet")


# --- argv builders ---------------------------------------------------------------------------


def spark_name() -> str:
    raise NotImplementedError("spark_name is not written yet")


def generator_argv(events: int, rate: float, procs: int, seed: int, knobs: bool) -> list[str]:
    raise NotImplementedError("generator_argv is not written yet")


def spark_argv(name: str) -> list[str]:
    raise NotImplementedError("spark_argv is not written yet")


def dag_trigger_argv() -> list[str]:
    raise NotImplementedError("dag_trigger_argv is not written yet")


def drain_argv(expect: int, timeout_s: float) -> list[str]:
    raise NotImplementedError("drain_argv is not written yet")


def sampler_argv(out: Path) -> list[str]:
    raise NotImplementedError("sampler_argv is not written yet")


def remove_argv(names: Sequence[str]) -> list[str]:
    raise NotImplementedError("remove_argv is not written yet")


# --- live window -----------------------------------------------------------------------------


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
    raise NotImplementedError("window is not written yet")


def build_parser() -> argparse.ArgumentParser:
    raise NotImplementedError("build_parser is not written yet")


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError("main is not written yet")


if __name__ == "__main__":
    sys.exit(main())

_UNUSED = (json, secrets, subprocess, threading, time, airflow_check, mem_report, compose_argv)
