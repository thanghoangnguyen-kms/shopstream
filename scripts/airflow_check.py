"""Item 6's measurement: one Kafka message per DAG run, timed against the message's own timestamp.

`produce` runs in the spark-job one-shot (confluent-kafka is in the spike image):
`python /app/airflow_check.py produce --value refresh-1`. `runs`, `events`, `wait-run` and `verdict`
run on the host and read the airflow database through `docker compose exec postgres psql` with fixed
argv lists, never a shell string. The last line of stdout is always one JSON line. Exit 0 on success,
1 when a wait timed out or the verdict is inconclusive, 2 on usage.

`item6_verdict` is the rule (ADR-001 go criterion 6): go only when two messages each match a distinct
asset_triggered run queued within 60 s of the message; fallback (the 5-minute schedule) when a message
gets no run, or a late one; inconclusive when fewer than two messages were sent, the runs or messages
cannot be parsed, or more runs than messages appear in the window. A run queued before the first
message (the tracer's) and a run of another run_type are ignored. The pure functions are unit-tested;
the live parts are exercised by the item 6 run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import cdc_check

REPO = Path(__file__).resolve().parents[1]
COMPOSE_ARGV = [
    "docker",
    "compose",
    "-f",
    str(REPO / "infra" / "compose.yaml"),
    "--profile",
    "core",
]
TOPIC = "fx.refresh"
DAG_ID = "fx_refresh_on_message"
RUN_TYPE = "asset_triggered"
LIMIT_S = 60
FALLBACK = "5-minute schedule"
POLL_S = 2.0
PSQL_TIMEOUT_S = 60
RUNS_SQL = (
    "SELECT coalesce(json_agg(row_to_json(r) ORDER BY r.id), '[]') FROM "
    "(SELECT id, run_id, run_type, state, queued_at, start_date FROM dag_run "
    "WHERE dag_id = 'fx_refresh_on_message') r"
)
EVENTS_SQL = (
    "SELECT coalesce(json_agg(row_to_json(e) ORDER BY e.id), '[]') FROM "
    '(SELECT id, "timestamp", extra FROM asset_event) e'
)
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def parse_iso_ms(text: str | None) -> int:
    """Epoch milliseconds for an ISO 8601 timestamp with a UTC offset or a `Z` suffix.

    Raises ValueError for None, for text that is not a timestamp and for a timestamp with no offset,
    which would be read in the wrong zone without a word.
    """
    if not isinstance(text, str):
        raise ValueError(f"not a timestamp: {text!r}")
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 timestamp: {text!r}") from exc
    if moment.tzinfo is None:
        raise ValueError(f"timestamp has no UTC offset: {text!r}")
    delta = moment - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _result(
    verdict: str, reasons: list[str], matches: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "fallback": FALLBACK if verdict == "fallback" else None,
        "reasons": reasons,
        "matches": matches or [],
    }


def message_ms(value: Any) -> int:
    """A message's Kafka timestamp in epoch milliseconds; raises ValueError for anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"not an epoch-millisecond timestamp: {value!r}")
    return value


def _start_latency_s(run: Mapping[str, Any], sent_ms: int) -> float | None:
    try:
        return round((parse_iso_ms(run.get("start_date")) - sent_ms) / 1000, 3)
    except ValueError:
        return None


def item6_verdict(
    messages: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    limit_s: float = LIMIT_S,
) -> dict[str, Any]:
    """go, fallback or inconclusive for the messages sent and the DAG's runs (see the module doc)."""
    if len(messages) < 2:
        return _result("inconclusive", [f"fewer than two messages were sent ({len(messages)})"])
    try:
        sent = sorted(
            ((message_ms(m["timestamp_ms"]), m) for m in messages),
            key=lambda pair: pair[0],
        )
    except (KeyError, TypeError, ValueError):
        return _result("inconclusive", ["the messages cannot be parsed (no timestamp_ms)"])
    try:
        asset_runs = sorted(
            ((parse_iso_ms(r.get("queued_at")), r) for r in runs if r.get("run_type") == RUN_TYPE),
            key=lambda pair: pair[0],
        )
    except (AttributeError, ValueError):
        return _result("inconclusive", ["the runs cannot be parsed (no usable queued_at)"])
    first_ms = sent[0][0]
    window = [pair for pair in asset_runs if pair[0] >= first_ms]
    if len(window) > len(messages):
        return _result(
            "inconclusive",
            [f"{len(window)} {RUN_TYPE} runs appeared in the window for {len(messages)} messages"],
        )
    reasons: list[str] = []
    matches: list[dict[str, Any]] = []
    unused = list(window)
    for sent_ms, message in sent:
        offset = message.get("offset")
        pick = next((pair for pair in unused if pair[0] >= sent_ms), None)
        if pick is None:
            reasons.append(f"the message at offset {offset} got no run")
            continue
        unused.remove(pick)
        queued_ms, run = pick
        latency_s = round((queued_ms - sent_ms) / 1000, 3)
        matches.append(
            {
                "offset": offset,
                "run_id": run.get("run_id"),
                "latency_s": latency_s,
                "start_latency_s": _start_latency_s(run, sent_ms),
                "state": run.get("state"),
            }
        )
        if latency_s > limit_s:
            reasons.append(
                f"the message at offset {offset} got run {run.get('run_id')} "
                f"{latency_s:g} s after it, over the {limit_s:g} s limit"
            )
    return _result("fallback" if reasons else "go", reasons, matches)


def psql_argv(sql: str) -> list[str]:
    """The fixed argv that runs one SQL text against the airflow database in the postgres service."""
    return [
        *COMPOSE_ARGV,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "airflow",
        "-At",
        "-c",
        sql,
    ]


def read_rows(sql: str) -> list[dict[str, Any]]:
    """The JSON array one of the aggregate queries prints, as a list of dicts."""
    out = subprocess.run(
        psql_argv(sql), capture_output=True, text=True, check=True, timeout=PSQL_TIMEOUT_S
    ).stdout
    data = json.loads(out.strip() or "[]")
    if not isinstance(data, list):
        raise ValueError("the query did not return a JSON array")
    return [row for row in data if isinstance(row, dict)]


def runs() -> list[dict[str, Any]]:
    return read_rows(RUNS_SQL)


def events() -> list[dict[str, Any]]:
    return read_rows(EVENTS_SQL)


def wait_run(after_ms: int, timeout_s: float, poll_s: float = POLL_S) -> dict[str, Any] | None:
    """The first asset_triggered run queued at or after `after_ms`, or None after `timeout_s`."""
    deadline = time.monotonic() + timeout_s
    while True:
        for run in runs():
            if run.get("run_type") != RUN_TYPE:
                continue
            try:
                queued_ms = parse_iso_ms(run.get("queued_at"))
            except ValueError:
                continue
            if queued_ms >= after_ms:
                return run
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


def produce(value: str) -> dict[str, Any]:
    """Send one message to fx.refresh and return where it landed and its Kafka timestamp."""
    from confluent_kafka import Producer

    delivered: list[dict[str, Any]] = []
    failures: list[str] = []

    def on_delivery(err: Any, msg: Any) -> None:
        if err is not None:
            failures.append(str(err))
            return
        delivered.append(
            {
                "topic": msg.topic(),
                "partition": msg.partition(),
                "offset": msg.offset(),
                "timestamp_ms": msg.timestamp()[1],
                "value": value,
            }
        )

    producer = Producer({"bootstrap.servers": cdc_check.BOOTSTRAP, "acks": "all"})
    producer.produce(TOPIC, value.encode("utf-8"), on_delivery=on_delivery)
    if producer.flush(30) or failures or len(delivered) != 1:
        raise RuntimeError(f"delivery failed: {failures or 'not acknowledged within 30 s'}")
    return delivered[0]


def read_messages(path: Path) -> list[dict[str, Any]]:
    """One JSON object per non-blank line, as `produce` prints them."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    messages = [json.loads(line) for line in lines]
    if not all(isinstance(message, dict) for message in messages):
        raise ValueError("a message line is not a JSON object")
    return messages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Item 6: Kafka messages against DAG runs.")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser(
        "produce", help="send one message to fx.refresh (in the spark-job one-shot)"
    )
    send.add_argument("--value", required=True)
    sub.add_parser("runs", help="print the fx_refresh_on_message runs as a JSON array")
    sub.add_parser("events", help="print the asset events as a JSON array")
    wait = sub.add_parser("wait-run", help="wait for an asset_triggered run queued after a time")
    wait.add_argument("--after-ms", type=int, required=True)
    wait.add_argument("--timeout", type=float, required=True)
    verdict = sub.add_parser(
        "verdict", help="item 6's verdict from a messages file and a runs file"
    )
    verdict.add_argument("--messages", type=Path, required=True)
    verdict.add_argument("--runs", type=Path, required=True)
    return parser


def _emit(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _verdict_command(messages_path: Path, runs_path: Path) -> int:
    try:
        messages = read_messages(messages_path)
        loaded = json.loads(runs_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError("the runs file is not a JSON array")
    except (OSError, ValueError) as exc:
        _emit(_result("inconclusive", [f"the input files cannot be read: {type(exc).__name__}"]))
        return 1
    result = item6_verdict(messages, loaded)
    _emit(result)
    return 1 if result["verdict"] == "inconclusive" else 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "produce":
            _emit(produce(args.value))
        elif args.command == "runs":
            _emit(runs())
        elif args.command == "events":
            _emit(events())
        elif args.command == "wait-run":
            if args.timeout <= 0:
                print("--timeout must be positive", file=sys.stderr)
                return 2
            found = wait_run(args.after_ms, args.timeout)
            if found is None:
                _emit({"found": False, "after_ms": args.after_ms})
                return 1
            queued_ms = parse_iso_ms(found["queued_at"])
            _emit({"found": True, "run": found, "latency_s": (queued_ms - args.after_ms) / 1000})
        else:
            return _verdict_command(args.messages, args.runs)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
