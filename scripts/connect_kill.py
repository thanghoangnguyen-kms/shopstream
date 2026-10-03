"""The reusable kill harness for item 5: SIGKILL the Connect worker at a commit stage, then recover.

Run on the host, never in a container: `uv run --frozen python scripts/connect_kill.py --stage ready
--delay-ms 200 --timeout 240`. It follows the Connect log, and on the first line for the chosen
commit stage it waits `--delay-ms`, sends SIGKILL to the `connect` service, starts it again, and waits
until both connectors report RUNNING and a new table commit has completed. A commit's window is
sub-second, so the kill triggers on a log line rather than a timer. The last line of stdout is one
compact JSON line (the kill record), which `cdc_check.py classify` and `item5 --kills-stdin` read on
stdin. Exit 0 when the worker recovered, 1 when recovery timed out, 2 on usage.

Stdlib only, and every command is a fixed docker argv list (see `COMPOSE_ARGV`), never a shell
string. The pure parts (`parse_line`, the argv builders, `is_running`) are unit-tested; the live
parts are exercised by the item 5 run. Stage lines (Iceberg sink 1.11.0's coordinator log):
`initiated commit <uuid>` (workers are writing), `Commit <uuid> ready, received responses for all N
partitions` (every DATA_COMPLETE is in and the table commit is next) and `completed commit to table`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
COMPOSE_ARGV = [
    "docker",
    "compose",
    "-f",
    str(REPO / "infra" / "compose.yaml"),
    "--profile",
    "core",
    "--profile",
    "streaming",
]
SERVICE = "connect"
CONNECTORS = ("shopstream-cdc", "bronze-sink")
CONNECT_STATUS_URL = "http://localhost:8083/connectors/{name}/status"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UUID_PATTERN = re.compile(UUID)
STAGES = {
    "initiated": re.compile(rf"initiated commit ({UUID})"),
    "ready": re.compile(rf"Commit ({UUID}) ready, received responses for all"),
    "completed": re.compile(r"completed commit to table"),
}
CONNECTOR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
STATUS_INTERVAL_S = 2.0
MATCHED_LINE_LIMIT = 160
RUNNING = "RUNNING"


def parse_line(line: str) -> tuple[str | None, str | None]:
    """(stage, commit id) for a Connect log line; (None, None) when it is not a stage line.

    The commit id is the stage's own uuid, else the first uuid on the line, else None (the
    `completed commit to table` line often names none).
    """
    for stage, pattern in STAGES.items():
        match = pattern.search(line)
        if match:
            if match.groups():
                return stage, match.group(1)
            found = UUID_PATTERN.search(line)
            return stage, found.group(0) if found else None
    return None, None


def logs_argv(since: str) -> list[str]:
    """`docker compose logs -f` for the connect service from `since` (a duration or a timestamp)."""
    return [*COMPOSE_ARGV, "logs", "-f", "--no-log-prefix", "--since", since, SERVICE]


def kill_argv() -> list[str]:
    return [*COMPOSE_ARGV, "kill", "-s", "KILL", SERVICE]


def start_argv() -> list[str]:
    return [*COMPOSE_ARGV, "start", SERVICE]


def status_argv(name: str) -> list[str]:
    """The curl inside the connect container that reads one connector's status."""
    if not CONNECTOR_NAME.match(name):
        raise ValueError("not a plain connector name")
    return [
        *COMPOSE_ARGV,
        "exec",
        "-T",
        SERVICE,
        "curl",
        "-fsS",
        CONNECT_STATUS_URL.format(name=name),
    ]


def is_running(report: Mapping[str, Any]) -> bool:
    """True when the connector and at least one task are RUNNING and no task is not."""
    connector = report.get("connector")
    if not isinstance(connector, dict) or connector.get("state") != RUNNING:
        return False
    tasks = report.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False
    return all(isinstance(task, dict) and task.get("state") == RUNNING for task in tasks)


def now_us() -> int:
    return time.time_ns() // 1000


def follow(argv: list[str]) -> tuple[subprocess.Popen[str], queue.Queue[str | None]]:
    """Start the log follower; its lines arrive on a queue, and None marks the end of the stream."""
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        if process.stdout is not None:
            for line in process.stdout:
                lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return process, lines


def wait_for_line(
    lines: queue.Queue[str | None], stage: str, deadline: float, last_initiated: list[str]
) -> tuple[str, str | None] | None:
    """The first line for `stage`: (line, commit id), or None on a timeout or the end of the log.

    Every initiated line updates `last_initiated[0]`, the commit id a stage line without one falls
    back to.
    """
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            continue
        if line is None:
            return None
        found, commit_id = parse_line(line)
        if found == "initiated" and commit_id:
            last_initiated[0] = commit_id
        if found == stage:
            return line.rstrip("\n"), commit_id
    return None


def connectors_running() -> bool:
    """True when both connectors answer a status call and report RUNNING."""
    for name in CONNECTORS:
        result = subprocess.run(status_argv(name), capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False
        try:
            report = json.loads(result.stdout)
        except ValueError:
            return False
        if not isinstance(report, dict) or not is_running(report):
            return False
    return True


def run_kill(stage: str, delay_ms: int, timeout_s: float) -> dict[str, Any]:
    """Kill the worker on the first `stage` line, restart it and wait for recovery; the kill record.

    Recovery is both connectors RUNNING and a new `completed commit to table` line after the
    restart. The record's `commit_id` is the matched line's id, else the last initiated id.
    """
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    record: dict[str, Any] = {
        "stage": stage,
        "delay_ms": delay_ms,
        "commit_id": None,
        "matched_line": None,
        "kill_ts_us": None,
        "start_ts_us": None,
        "recovered_ts_us": None,
        "recovered": False,
    }
    last_initiated: list[str] = [""]
    deadline = time.monotonic() + timeout_s
    process, lines = follow(logs_argv("1s"))
    try:
        matched = wait_for_line(lines, stage, deadline, last_initiated)
        if matched is None:
            return record
        line, commit_id = matched
        time.sleep(delay_ms / 1000)
        subprocess.run(kill_argv(), check=True, capture_output=True)
        record["kill_ts_us"] = now_us()
        record["matched_line"] = line[:MATCHED_LINE_LIMIT]
        record["commit_id"] = commit_id or last_initiated[0] or None
    finally:
        process.terminate()
    restarted = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run(start_argv(), check=True, capture_output=True)
    record["start_ts_us"] = now_us()
    while time.monotonic() < deadline and not connectors_running():
        time.sleep(STATUS_INTERVAL_S)
    process, lines = follow(logs_argv(restarted))
    try:
        if wait_for_line(lines, "completed", deadline, last_initiated) is not None:
            record["recovered_ts_us"] = now_us()
            record["recovered"] = True
    finally:
        process.terminate()
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SIGKILL the Connect worker at a commit stage.")
    parser.add_argument("--stage", required=True, choices=sorted(STAGES))
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=240.0, help="seconds, for each wait")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    if args.delay_ms < 0 or args.timeout <= 0:
        print("--delay-ms must not be negative and --timeout must be positive", file=sys.stderr)
        return 2
    try:
        record = run_kill(args.stage, args.delay_ms, args.timeout)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(record, sort_keys=True), flush=True)
    return 0 if record["recovered"] else 1


if __name__ == "__main__":
    sys.exit(main())
