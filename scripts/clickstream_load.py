"""The throwaway clickstream generator for item 12 (and, later, item 13's knobs), run by `cdc-run`.

ADR-001's Evidence rules exempt a one-off load script from unit tests, so this file has none; the
evidence page records its command line and its seed. Its one pure rule, `offset_runs`, lives in
throughput_check.py, where it is tested, so the generator's runs and bronze's islands come from one rule.

Mode: `run --events N --rate R --procs P --seed S` sends N Avro events to the `clickstream` topic.
P (1, 2, 3 or 6) worker processes each own the partitions p with p % P == worker and send their events
to those partitions round-robin with an explicit `partition=`; each builds its own Producer after the
fork (idempotent, acks all, lz4, linger 20 ms, batch 128 KiB). R is the total events per second
(0 is unthrottled). Event values come from `random.Random(seed * 1000 + worker)`, so a seed repeats a
batch exactly: event_id is `<seed>-<worker>-<n>`, event_time is 2026-01-01T00:00:00Z plus n x 50 ms per
worker, `referrer` is drawn from search, email, social and ads (never the schema default "direct", the
apache/iceberg#17652 null-default carrier) and `tag` is null in every event here. The wire format is
Confluent's: 0x00, a big-endian schema id from Karapace, then the Avro binary. The key is the event_id
string, registered under `clickstream-key` (the value under `clickstream-value`).

The delivery callback records every acknowledged offset per partition; the report holds the offset
runs (through throughput_check.offset_runs), duplicates and counts, Kafka's latest offsets before and
after, and `offsets_consistent` (each partition's count equals its end minus its start). It carries
counts, offsets, runs, timestamps and schema ids only: never an event field value and never the
canary. The last line of stdout is one JSON line; exit 0 on success, 1 on an error (the message is
scrubbed of CANARY_TOKEN), 2 on usage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import multiprocessing
import os
import random
import sys
import time
from array import array
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import cdc_check
import connect_admin
from read_v3 import error_text
from throughput_check import offset_runs

TOPIC = "clickstream"
PARTITIONS = 6
PROCS = (1, 2, 3, 6)
CANARY_ENV = "CANARY_TOKEN"
KEY_SCHEMA = "string"
# The report's schema_ids are keyed by subject, so no report key is a bare "value".
KEY_SUBJECT = "clickstream-key"
VALUE_SUBJECT = "clickstream-value"
CLICKSTREAM_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "clickstream_event",
    "namespace": "shopstream.spike",
    "fields": [
        {"name": "event_id", "type": "string"},
        {"name": "session_id", "type": "string"},
        {"name": "customer_id", "type": "long"},
        {"name": "product_id", "type": "long"},
        {"name": "event_type", "type": "string"},
        {"name": "event_time", "type": {"type": "long", "logicalType": "timestamp-millis"}},
        {"name": "page", "type": "string"},
        {"name": "user_agent", "type": "string"},
        {"name": "referrer", "type": ["string", "null"], "default": "direct"},
        {"name": "tag", "type": ["null", "string"], "default": None},
    ],
}
PRODUCER_SETTINGS = {
    "enable.idempotence": "true",
    "acks": "all",
    "compression.type": "lz4",
    "linger.ms": "20",
    "batch.size": "131072",
}
EVENT_TYPES = ("view", "view", "view", "add_to_cart", "purchase")
USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
)
REFERRERS = ("search", "email", "social", "ads")
EVENT_EPOCH_MS = int(dt.datetime(2026, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)
EVENT_STEP_MS = 50
SESSION_SIZE = 20
CUSTOMERS = 50_000
PRODUCTS = 10_000
POLL_EVERY = 500
PACE_EVERY = 200
FLUSH_TIMEOUT_S = 600
ADMIN_TIMEOUT_S = 30


def register_schemas() -> dict[str, int]:
    """Post the key and value schemas to Karapace; return {subject: id} for both subjects.

    The same schema text returns the same id, so a repeat registers nothing new.
    """
    ids: dict[str, int] = {}
    for subject, schema in (
        (KEY_SUBJECT, json.dumps(KEY_SCHEMA)),
        (VALUE_SUBJECT, json.dumps(CLICKSTREAM_SCHEMA)),
    ):
        status, payload = connect_admin.http_request(
            "POST",
            f"{connect_admin.KARAPACE_URL}/subjects/{subject}/versions",
            {"Content-Type": "application/vnd.schemaregistry.v1+json"},
            json.dumps({"schema": schema}).encode(),
        )
        parsed = json.loads(payload) if payload else {}
        if status != 200 or not isinstance(parsed, dict) or "id" not in parsed:
            raise RuntimeError(f"Karapace returned HTTP {status} for subject {subject}")
        ids[subject] = int(parsed["id"])
    return ids


def encode(schema_id: int, parsed_schema: Any, record: Any) -> bytes:
    """Confluent wire format: 0x00, the schema id (4 bytes, big endian), the Avro binary."""
    import io

    import fastavro

    buffer = io.BytesIO()
    buffer.write(b"\x00" + schema_id.to_bytes(4, "big"))
    fastavro.schemaless_writer(buffer, parsed_schema, record)
    return buffer.getvalue()


def make_event(rng: random.Random, seed: int, worker: int, n: int) -> dict[str, Any]:
    """Event `n` of `worker`: every value from the worker's seeded generator."""
    product_id = rng.randint(1, PRODUCTS)
    return {
        "event_id": f"{seed}-{worker}-{n}",
        "session_id": f"s{worker}-{n // SESSION_SIZE}",
        "customer_id": rng.randint(1, CUSTOMERS),
        "product_id": product_id,
        "event_type": EVENT_TYPES[rng.randrange(len(EVENT_TYPES))],
        "event_time": EVENT_EPOCH_MS + n * EVENT_STEP_MS,
        "page": f"/p/{product_id}",
        "user_agent": USER_AGENTS[rng.randrange(len(USER_AGENTS))],
        "referrer": REFERRERS[rng.randrange(len(REFERRERS))],
        "tag": None,
    }


def worker_partitions(worker: int, procs: int) -> list[int]:
    return [p for p in range(PARTITIONS) if p % procs == worker]


def events_for(worker: int, procs: int, total: int) -> int:
    """The share of `total` events the worker sends: the first `total % procs` workers get one more."""
    return total // procs + (1 if worker < total % procs else 0)


def _worker(spec: Mapping[str, Any]) -> dict[str, Any]:
    """One producer process: send its events, wait for every acknowledgement, return its offsets."""
    import fastavro
    from confluent_kafka import Producer

    worker = int(spec["worker"])
    seed = int(spec["seed"])
    count = int(spec["events"])
    partitions: list[int] = list(spec["partitions"])
    rate = float(spec["rate"])
    value_schema = fastavro.parse_schema(CLICKSTREAM_SCHEMA)
    key_schema = fastavro.parse_schema(KEY_SCHEMA)
    key_id = int(spec["key_id"])
    value_id = int(spec["value_id"])
    offsets: dict[int, array[int]] = {p: array("q") for p in partitions}
    failures = [0]

    def on_delivery(err: Any, msg: Any) -> None:
        if err is not None:
            failures[0] += 1
            return
        offsets[msg.partition()].append(msg.offset())

    producer = Producer(
        {
            "bootstrap.servers": cdc_check.BOOTSTRAP,
            "queue.buffering.max.messages": 500_000,
            **PRODUCER_SETTINGS,
        }
    )
    rng = random.Random(seed * 1000 + worker)
    started_ms = int(time.time() * 1000)
    begun = time.perf_counter()
    for n in range(count):
        event = make_event(rng, seed, worker, n)
        key = encode(key_id, key_schema, event["event_id"])
        value = encode(value_id, value_schema, event)
        partition = partitions[n % len(partitions)]
        while True:
            try:
                producer.produce(
                    TOPIC, value=value, key=key, partition=partition, on_delivery=on_delivery
                )
                break
            except BufferError:
                producer.poll(0.1)
        if n % POLL_EVERY == 0:
            producer.poll(0)
        if rate > 0 and n % PACE_EVERY == 0:
            ahead = begun + n / rate - time.perf_counter()
            if ahead > 0:
                time.sleep(ahead)
    remaining = producer.flush(FLUSH_TIMEOUT_S)
    ended_ms = int(time.time() * 1000)
    per_partition = {}
    for p, taken in offsets.items():
        runs, duplicates = offset_runs(taken)
        per_partition[str(p)] = {"count": len(taken), "runs": runs, "duplicates": duplicates}
    return {
        "worker": worker,
        "started_at_ms": started_ms,
        "ended_at_ms": ended_ms,
        "failed": failures[0] + remaining,
        "per_partition": per_partition,
    }


def latest_offsets() -> dict[int, int]:
    """Each clickstream partition's latest (next) offset, from Kafka."""
    from confluent_kafka import TopicPartition
    from confluent_kafka.admin import AdminClient, OffsetSpec

    admin = AdminClient({"bootstrap.servers": cdc_check.BOOTSTRAP})
    request = {TopicPartition(TOPIC, p): OffsetSpec.latest() for p in range(PARTITIONS)}
    futures = admin.list_offsets(request, request_timeout=ADMIN_TIMEOUT_S)
    return {tp.partition: int(f.result().offset) for tp, f in futures.items()}


def run(events: int, rate: float, procs: int, seed: int, command: str) -> dict[str, Any]:
    ids = register_schemas()
    before = latest_offsets()
    specs = [
        {
            "worker": w,
            "procs": procs,
            "seed": seed,
            "events": events_for(w, procs, events),
            "rate": rate / procs,
            "partitions": worker_partitions(w, procs),
            "key_id": ids[KEY_SUBJECT],
            "value_id": ids[VALUE_SUBJECT],
        }
        for w in range(procs)
    ]
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=procs, mp_context=context) as pool:
        results = list(pool.map(_worker, specs))
    after = latest_offsets()
    per_partition: dict[str, dict[str, Any]] = {}
    for result in results:
        for p, facts in result["per_partition"].items():
            start, end = before[int(p)], after[int(p)]
            per_partition[p] = {"start": start, "end": end, **facts}
    delivered = sum(int(f["count"]) for f in per_partition.values())
    started = min(int(r["started_at_ms"]) for r in results)
    ended = max(int(r["ended_at_ms"]) for r in results)
    span_ms = max(ended - started, 1)
    return {
        "command": command,
        "seed": seed,
        "events_requested": events,
        "delivered": delivered,
        "failed": sum(int(r["failed"]) for r in results),
        "started_at_ms": started,
        "ended_at_ms": ended,
        "achieved_rate": round(delivered * 1000 / span_ms, 1),
        "procs": procs,
        "producer": dict(PRODUCER_SETTINGS),
        "schema_ids": ids,
        "per_partition": dict(sorted(per_partition.items(), key=lambda kv: int(kv[0]))),
        "offsets_consistent": all(
            int(f["count"]) == int(f["end"]) - int(f["start"]) for f in per_partition.values()
        ),
        "knobs": {},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seeded clickstream generator for item 12.")
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="send --events Avro events to the clickstream topic")
    go.add_argument("--events", type=int, required=True)
    go.add_argument("--rate", type=float, default=20_000.0, help="total events/s; 0 is unthrottled")
    go.add_argument("--procs", type=int, default=2, help="producer processes: 1, 2, 3 or 6")
    go.add_argument("--seed", type=int, default=1)
    return parser


def usage_error(args: argparse.Namespace) -> str | None:
    if args.events < 1:
        return "--events must be at least 1"
    if args.rate < 0:
        return "--rate must not be negative"
    if args.procs not in PROCS:
        return "--procs must be 1, 2, 3 or 6"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(given)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    problem = usage_error(args)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    try:
        report = run(
            args.events,
            args.rate,
            args.procs,
            args.seed,
            "python /app/clickstream_load.py " + " ".join(given),
        )
    except Exception as exc:
        print(f"error: {error_text(exc, [os.environ.get(CANARY_ENV, '')])}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
