"""Item 5's checks: find a Postgres row in bronze, and measure exactly-once against two ground truths.

Run inside the `cdc-run` one-shot (the spike image on the Compose network, which also reaches Kafka,
Karapace and Connect): `python /app/cdc_check.py item5 --timeout 300`. The last line of stdout is one
compact JSON line. Plan 04-01's tracer used `ensure-namespace`, `refs` and `bronze-find`; Plan 04-02
adds the exactly-once measurement (`item5`), the `test_decoding` count slot (`slot-create`,
`slot-drop`) and the scoped `reset`; Plan 04-03 adds the rollback checks.

Item 5's rule is pure and tested: `pk_of`, `compare_offsets`, `change_stats`, `delete_stats`,
`count_changes` and `item5_verdict`. The bronze set of (topic, partition, offset) is compared with the
non-null records a `read_committed` consumer reads below each partition's fixed end offset, and the
distinct (table, primary key, source.lsn) changes are compared with the INSERT, UPDATE and DELETE
lines a `test_decoding` slot decodes for the five captured tables. A change is not a row: Debezium
re-sends a change after a worker restart at a new offset with the same source.lsn, so count(*) above
the distinct changes is reported and never a failure.

This is a spike script (ADR-001 Evidence rules): the live readers need a running stack and have no
unit tests. The heavy imports (pyiceberg, confluent_kafka, psycopg) sit inside functions, so
importing this module in CI is safe. Every REST call goes through an origin-allowlisted client, every
table name through `read_v3.identifier`, and no vended credential, password or row value is printed:
the report holds counts, synthetic integer keys, offsets and LSNs.

The bronze tables hold the raw Debezium envelope (`before`, `after`, `source`, `op`, ...) plus the
KafkaMetadataTransform columns, so a row's primary key comes from `after`, or from `before` for a delete.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import connect_admin
import gold_reset
from read_v3 import error_text, identifier, package_version
from spark_v3_job import WAREHOUSE, catalog_prefix, http_request, lakekeeper_url

NAMESPACE = "bronze"
PK_COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": ("customer_id",),
    "products": ("product_id",),
    "orders": ("order_id",),
    "order_items": ("order_id", "line_no"),
    "reviews": ("review_id",),
}
KAFKA_META_COLUMNS = (
    "_kafka_metadata_topic",
    "_kafka_metadata_partition",
    "_kafka_metadata_offset",
    "_kafka_metadata_timestamp",
)
# A bronze row's identity is its Kafka coordinates, never the writer and never (key, lsn).
OFFSET_COLUMNS = KAFKA_META_COLUMNS[:3]
BOOTSTRAP_DEFAULT = "kafka-1:19092,kafka-2:19092,kafka-3:19092"
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", BOOTSTRAP_DEFAULT)
AUDIT_BRANCH = "audit"
POLL_INTERVAL_S = 5.0
JSON_HEADERS = {"Content-Type": "application/json"}
ENVELOPE_COLUMNS = ("before", "after", "source", "op", *KAFKA_META_COLUMNS)
# A kill classified as one of these hit the commit window: after the workers' DATA_COMPLETE and
# before the table's COMMIT_COMPLETE. `classify_kill` reads the control topic to say which.
IN_WINDOW = frozenset({"data_complete", "commit_to_table"})
COUNT_SLOT = "cnt_slot"
DEBEZIUM_SLOT = "shopstream_dbz"
SAMPLE_LIMIT = 20
HEAD_LIMIT = 10
SETTLE_S = 10.0
CATCH_UP_INTERVAL_S = 10.0
# test_decoding's line for a change to one of the five captured tables.
TD_CHANGE = re.compile(
    r"^table public\.(customers|products|orders|order_items|reviews): (INSERT|UPDATE|DELETE):"
)
SECRET_ENV = ("SHOPSTREAM_DB_PASSWORD", "CDC_DB_PASSWORD")
PG_ROLES = {"shopstream": "SHOPSTREAM_DB_PASSWORD", "cdc": "CDC_DB_PASSWORD"}
COUNT_KEYS = (
    "kafka_nonnull",
    "missing",
    "missing_allowed",
    "extra",
    "duplicate_offsets",
    "distinct_changes",
    "pg_changes",
    "delete_ok",
)

Offset = tuple[str, int, int]
Change = tuple[str, tuple[int, ...] | None, int | None]


# --- pure rule --------------------------------------------------------------------------------


def pk_of(
    table: str, after: Mapping[str, Any] | None, before: Mapping[str, Any] | None
) -> tuple[int, ...] | None:
    """The row's primary key as ints, from `after`, else `before`; None when both are null."""
    image = after if isinstance(after, Mapping) else before if isinstance(before, Mapping) else None
    if image is None:
        return None
    return tuple(int(image[column]) for column in PK_COLUMNS[table])


def offset_key(row: Mapping[str, Any]) -> Offset:
    """A bronze row's (topic, partition, offset), read through KAFKA_META_COLUMNS[:3] as ints."""
    topic, partition, offset = (row[column] for column in OFFSET_COLUMNS)
    return str(topic), int(partition), int(offset)


def compare_offsets(
    kafka: set[Offset],
    bronze: Sequence[Offset],
    end_offsets: Mapping[tuple[str, int], int],
    allowed_missing: frozenset[Offset] = frozenset(),
) -> dict[str, Any]:
    """Compare the read_committed Kafka set with bronze's offsets, as sets, below the end offsets.

    `end_offsets` maps (topic, partition) to the exclusive end: a bronze row at or beyond it is
    `beyond_end`, never `extra`; a partition with no entry has no records, so its rows are beyond.
    `missing` is every Kafka offset bronze lacks; `missing_allowed` is the part of it named in
    `allowed_missing` (an erased row). A repeated offset is one duplicate, however often it repeats.
    Samples are sorted by (topic, partition, offset) and cut to 20 beside the full counts.
    """
    in_range: set[Offset] = set()
    beyond = 0
    for topic, partition, offset in bronze:
        if offset < end_offsets.get((topic, partition), 0):
            in_range.add((topic, partition, offset))
        else:
            beyond += 1
    seen = Counter(bronze)
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    missing = sorted(kafka - in_range)
    extra = sorted(in_range - kafka)
    return {
        "kafka_nonnull": len(kafka),
        "bronze_in_range": len(in_range),
        "beyond_end": beyond,
        "missing": len(missing),
        "missing_allowed": len(set(missing) & allowed_missing),
        "extra": len(extra),
        "duplicate_offsets": len(duplicates),
        "missing_sample": [list(key) for key in missing[:SAMPLE_LIMIT]],
        "extra_sample": [list(key) for key in extra[:SAMPLE_LIMIT]],
        "duplicate_sample": [list(key) for key in duplicates[:SAMPLE_LIMIT]],
    }


def change_stats(rows: Iterable[Change]) -> dict[str, int]:
    """Rows, distinct changes and Debezium re-sends over (table, primary key, source.lsn)."""
    changes = list(rows)
    distinct = len(set(changes))
    return {
        "bronze_rows": len(changes),
        "distinct_changes": distinct,
        "resends": len(changes) - distinct,
    }


def delete_stats(
    d_changes: Iterable[Change],
    pg_deletes: int,
    bronze_offsets: Iterable[Offset],
    tombstone_offsets: Iterable[Offset],
) -> dict[str, Any]:
    """Check deletes: one distinct op=d change per Postgres DELETE, and no row at a tombstone.

    `d_changes` are the (table, primary key, source.lsn) of every op=d bronze row, so a Debezium
    re-send of one delete is one change. A tombstone has a null value, which the sink skips, so a
    bronze row at a tombstone's offset would be a row the sink should never have written.
    """
    tombstones = set(tombstone_offsets)
    at_tombstones = sum(1 for offset in bronze_offsets if offset in tombstones)
    distinct = len(set(d_changes))
    return {
        "pg_deletes": pg_deletes,
        "distinct_deletes": distinct,
        "rows_at_tombstones": at_tombstones,
        "delete_ok": distinct == pg_deletes and at_tombstones == 0,
    }


def count_changes(lines: Iterable[str]) -> dict[str, Any]:
    """Count the INSERT, UPDATE and DELETE lines of the five captured tables in test_decoding output.

    BEGIN and COMMIT lines and lines for any other table are ignored. `by_table_op` keys read
    `customers:INSERT`.
    """
    by_table_op: Counter[str] = Counter()
    for line in lines:
        match = TD_CHANGE.match(line)
        if match:
            by_table_op[f"{match.group(1)}:{match.group(2)}"] += 1
    return {
        "total": sum(by_table_op.values()),
        "deletes": sum(n for key, n in by_table_op.items() if key.endswith(":DELETE")),
        "by_table_op": dict(sorted(by_table_op.items())),
    }


def delete_example(
    rows: Sequence[Mapping[str, Any]], tombstones: set[Offset]
) -> dict[str, Any] | None:
    """One deleted key whose op=d change was never re-sent: its rows and its tombstone's offset.

    The tombstone is the next tombstone offset in the delete row's partition: the single source
    task writes the delete and its tombstone one after the other. Keys and offsets only.
    """
    deleted = [row for row in rows if row["op"] == "d" and row["pk"] is not None]
    row_offsets = {(r["topic"], r["partition"], r["offset"]) for r in rows}
    counts = Counter((row["table"], row["pk"]) for row in deleted)
    for row in sorted(deleted, key=lambda r: (r["topic"], r["partition"], r["offset"])):
        key = (row["table"], row["pk"])
        if counts[key] != 1:
            continue
        later = sorted(
            offset
            for topic, partition, offset in tombstones
            if (topic, partition) == (row["topic"], row["partition"]) and offset > row["offset"]
        )
        history = sorted(
            (r for r in rows if (r["table"], r["pk"]) == key),
            key=lambda r: (r["partition"], r["offset"]),
        )
        return {
            "table": row["table"],
            "key": list(row["pk"]),
            "rows": [
                {"op": r["op"], "partition": r["partition"], "offset": r["offset"]}
                for r in history[:HEAD_LIMIT]
            ],
            "delete_offset": [row["partition"], row["offset"]],
            "tombstone_offset": later[0] if later else None,
            "tombstone_adjacent": bool(later) and later[0] == row["offset"] + 1,
            "tombstone_has_row": bool(later)
            and (row["topic"], row["partition"], later[0]) in row_offsets,
        }
    return None


CONTROL_MAGIC = b"\xc2\x01"
CONTROL_TYPES = {
    0: "START_COMMIT",
    1: "DATA_WRITTEN",
    2: "DATA_COMPLETE",
    3: "COMMIT_TO_TABLE",
    4: "COMMIT_COMPLETE",
}
UUID_BYTES = 16


def zigzag_long(data: bytes, index: int) -> tuple[int, int]:
    """Decode an Avro zigzag varint long at `index`: (value, the index after it).

    Raises IndexError when the bytes end inside the varint.
    """
    shift = 0
    number = 0
    while True:
        byte = data[index]
        index += 1
        number |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return (number >> 1) ^ -(number & 1), index
        shift += 7


def decode_control_head(data: bytes) -> dict[str, Any]:
    """The head of an Iceberg control event: type, timestamp (us), group id and commit id.

    The record is the magic 0xC2 0x01, a 2-byte big-endian length and that many bytes of embedded
    schema JSON (Java writeUTF), a 16-byte event id, a zigzag type, a zigzag timestamp in
    microseconds, a zigzag group-id length and the group id, then the payload, whose first field is
    the 16-byte commit id. A full Avro decode fails on this schema, so only this prefix is parsed.
    Raises ValueError for a missing magic, a short record or an unknown type.
    """
    if data[:2] != CONTROL_MAGIC:
        raise ValueError("not a control event: the magic bytes are missing")
    try:
        schema_length = int.from_bytes(data[2:4], "big")
        index = 4 + schema_length + UUID_BYTES
        type_id, index = zigzag_long(data, index)
        timestamp_us, index = zigzag_long(data, index)
        group_length, index = zigzag_long(data, index)
        group = data[index : index + group_length].decode()
        index += group_length
    except IndexError as exc:
        raise ValueError("the control event is too short") from exc
    commit = data[index : index + UUID_BYTES]
    if group_length < 0 or len(commit) != UUID_BYTES:
        raise ValueError("the control event is too short")
    if type_id not in CONTROL_TYPES:
        raise ValueError(f"unknown control event type {type_id}")
    return {
        "type": CONTROL_TYPES[type_id],
        "timestamp_us": timestamp_us,
        "group": group,
        "commit_id": str(uuid.UUID(bytes=commit)),
    }


def classify_kill(commit_id: str | None, events: Sequence[Mapping[str, Any]]) -> str:
    """Which commit stage a kill hit, from the control events that exist for its commit id.

    Worker and coordinator die together with the container, so every event on the topic for the
    killed commit was written before the kill. COMMIT_COMPLETE present: commit_complete (the kill
    came after the commit finished and proves nothing about recovery); COMMIT_TO_TABLE without it:
    commit_to_table; DATA_COMPLETE without COMMIT_TO_TABLE: data_complete; only START_COMMIT or
    DATA_WRITTEN: workers_writing. No commit id or no event for it: unknown.
    """
    if not commit_id:
        return "unknown"
    seen = {str(e["type"]) for e in events if e.get("commit_id") == commit_id}
    if not seen:
        return "unknown"
    if "COMMIT_COMPLETE" in seen:
        return "commit_complete"
    if "COMMIT_TO_TABLE" in seen:
        return "commit_to_table"
    if "DATA_COMPLETE" in seen:
        return "data_complete"
    return "workers_writing"


def kill_window(commit_id: str | None, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The events of the killed commit and of the next commit after it, in topic order.

    Each is {"type", "commit" (the commit id's first 8 characters), "timestamp_us"}. Empty when the
    commit id is missing or has no event.
    """
    if not commit_id:
        return []
    indexes = [i for i, e in enumerate(events) if e.get("commit_id") == commit_id]
    if not indexes:
        return []
    following = next(
        (e["commit_id"] for e in events[indexes[-1] + 1 :] if e.get("commit_id") != commit_id), None
    )
    wanted = {commit_id, following}
    return [
        {
            "type": str(e["type"]),
            "commit": str(e["commit_id"])[:8],
            "timestamp_us": int(e["timestamp_us"]),
        }
        for e in events
        if e.get("commit_id") in wanted
    ]


def classify_kills(
    kills: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Each kill record with `classified` and `events` (its kill window) attached; inputs unchanged."""
    return [
        {
            **kill,
            "classified": classify_kill(kill.get("commit_id"), events),
            "events": kill_window(kill.get("commit_id"), events),
        }
        for kill in kills
    ]


def item5_verdict(report: Mapping[str, Any]) -> dict[str, Any]:
    """Item 5's exactly-once rule: go, fallback ("lost change" or "offset gap") or inconclusive.

    Inconclusive when an input is missing, the Kafka set is empty, a table could not be read, or no
    kill landed in the commit window (IN_WINDOW). Fallback when a change is lost (distinct changes
    below the Postgres change count) or an offset is missing without an erasure explanation. Go only
    when set equality holds, no offset repeats, distinct changes equal the change count and the
    delete check holds. count(*) above the distinct changes is Debezium re-sends and never a
    failure. A duplicate offset, an extra offset or a failed delete check without a loss is
    inconclusive: ADR-001 names no fallback for it.
    """
    problems: list[str] = []
    counts = report.get("counts")
    if not isinstance(counts, Mapping):
        return {
            "verdict": "inconclusive",
            "fallback": None,
            "reasons": ["the report has no counts"],
        }
    absent = [key for key in COUNT_KEYS if key not in counts]
    if absent:
        problems.append(f"the counts lack {', '.join(absent)}")
    unreadable = report.get("unreadable_tables") or []
    if unreadable:
        problems.append(f"bronze table(s) could not be read: {', '.join(sorted(unreadable))}")
    if not absent and counts["kafka_nonnull"] == 0:
        problems.append("the Kafka set is empty, so there is nothing to compare")
    kills = report.get("kills") or []
    if not any(kill.get("classified") in IN_WINDOW for kill in kills):
        problems.append(
            f"no kill landed in the commit window (between DATA_COMPLETE and COMMIT_COMPLETE); "
            f"{len(kills)} kill(s) recorded"
        )
    if absent:
        return {"verdict": "inconclusive", "fallback": None, "reasons": problems}
    lost = counts["distinct_changes"] < counts["pg_changes"]
    gap = counts["missing"] > counts["missing_allowed"]
    findings: list[str] = []
    if lost:
        findings.append(
            f"a change is lost: {counts['distinct_changes']} distinct changes in bronze against "
            f"{counts['pg_changes']} in test_decoding"
        )
    if gap:
        findings.append(
            f"{counts['missing'] - counts['missing_allowed']} Kafka offset(s) are missing from "
            f"bronze without an erasure explanation"
        )
    if problems:
        return {"verdict": "inconclusive", "fallback": None, "reasons": problems + findings}
    if lost or gap:
        return {
            "verdict": "fallback",
            "fallback": "lost change" if lost else "offset gap",
            "reasons": findings,
        }
    other: list[str] = []
    if counts["duplicate_offsets"]:
        other.append(f"{counts['duplicate_offsets']} offset(s) appear more than once in bronze")
    if counts["extra"]:
        other.append(f"{counts['extra']} bronze offset(s) are not in the Kafka set")
    if counts["distinct_changes"] > counts["pg_changes"]:
        other.append("bronze holds more distinct changes than test_decoding counted")
    if not counts["delete_ok"]:
        other.append(
            "the delete check failed (a delete count differs or a row sits at a tombstone)"
        )
    if other:
        return {"verdict": "inconclusive", "fallback": None, "reasons": other}
    return {
        "verdict": "go",
        "fallback": None,
        "reasons": [
            f"{counts['kafka_nonnull']} non-null Kafka records equal bronze's offsets with no "
            f"duplicate offset, and {counts['distinct_changes']} distinct changes equal the "
            f"{counts['pg_changes']} test_decoding changes"
        ],
    }


# --- bronze reader ----------------------------------------------------------------------------


def bronze_catalog() -> Any:
    """The Lakekeeper REST catalog through PyIceberg, with vended credentials (never printed)."""
    from pyiceberg.catalog import load_catalog

    return load_catalog(
        "rest",
        uri=f"{lakekeeper_url()}/catalog",
        warehouse=WAREHOUSE,
        **{"header.X-Iceberg-Access-Delegation": "vended-credentials"},
    )


def ensure_namespace() -> None:
    """Create the bronze namespace through the REST API; 200 or 409 (it exists) are both fine."""
    body = json.dumps({"namespace": [identifier(NAMESPACE)], "properties": {}}).encode()
    url = f"{lakekeeper_url()}/catalog/v1/{catalog_prefix()}/namespaces"
    status, payload = http_request("POST", url, JSON_HEADERS, body)
    if status not in {200, 409}:
        raise RuntimeError(
            f"creating namespace {NAMESPACE} returned HTTP {status}: "
            f"{payload.decode(errors='replace')[:200]}"
        )


def load_bronze(table: str) -> Any:
    return bronze_catalog().load_table(f"{identifier(NAMESPACE)}.{identifier(table)}")


def refs(table: str) -> dict[str, int]:
    """The table's branch and tag names with their snapshot ids; a fresh sink table has no main."""
    return {name: int(ref.snapshot_id) for name, ref in load_bronze(table).refs().items()}


def key_of(row: Mapping[str, Any], table: str) -> tuple[str, ...] | None:
    """The row's primary key as strings, from `after`, else `before`; None when both are null."""
    image = row.get("after") or row.get("before")
    if not isinstance(image, Mapping):
        return None
    return tuple(str(image[column]) for column in PK_COLUMNS[table])


def parse_pk(table: str, text: str) -> tuple[str, ...]:
    """`--pk` as a tuple: one value, or comma-separated values in PK_COLUMNS order."""
    parts = tuple(part.strip() for part in text.split(","))
    if len(parts) != len(PK_COLUMNS[table]) or not all(parts):
        raise ValueError(f"--pk needs {len(PK_COLUMNS[table])} value(s) for {table}")
    return parts


def has_struct(iceberg_table: Any, column: str) -> bool:
    field = next((f for f in iceberg_table.schema().fields if f.name == column), None)
    return field is not None and "struct" in str(field.field_type).lower()


def bronze_find(table: str, pk: tuple[str, ...], timeout_s: float) -> dict[str, Any]:
    """Poll the audit branch every 5 s until a row with primary key `pk` lands; return its facts.

    The sink creates the table on its first commit, so a missing table is polled, not an error.
    When several rows match (an insert then updates) the one with the greatest source.lsn wins.
    `has_before_after` says the table holds both envelope images as structs (a create's `before` is
    null by design). Raises TimeoutError when nothing lands in time.
    """
    if table not in PK_COLUMNS:
        raise ValueError(f"unknown table {table}")
    from pyiceberg.exceptions import NoSuchTableError

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            iceberg_table = load_bronze(table)
        except NoSuchTableError:
            # The sink creates the table on its first commit, so the table may not exist yet.
            iceberg_table = None
        found = iceberg_table.refs() if iceberg_table is not None else {}
        if iceberg_table is not None and AUDIT_BRANCH in found:
            scan = iceberg_table.scan(
                snapshot_id=int(found[AUDIT_BRANCH].snapshot_id), selected_fields=ENVELOPE_COLUMNS
            )
            matches = [
                row
                for row in scan.to_arrow().to_pylist()
                if key_of(row, table) == pk and row.get("source") is not None
            ]
            if matches:
                best = max(matches, key=lambda row: int(row["source"]["lsn"]))
                return {
                    "table": table,
                    "refs": {name: int(ref.snapshot_id) for name, ref in found.items()},
                    "op": best["op"],
                    "lsn": int(best["source"]["lsn"]),
                    "partition": int(best["_kafka_metadata_partition"]),
                    "offset": int(best["_kafka_metadata_offset"]),
                    "has_before_after": has_struct(iceberg_table, "before")
                    and has_struct(iceberg_table, "after"),
                    "before_is_null": best["before"] is None,
                    "matches": len(matches),
                }
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"no bronze.{table} row with key {','.join(pk)} on {AUDIT_BRANCH} "
                f"after {timeout_s:g} s (refs: {sorted(found)})"
            )
        time.sleep(POLL_INTERVAL_S)


def bronze_rows(table: str, ref: str = AUDIT_BRANCH) -> dict[str, Any]:
    """The rows of bronze.<table> at `ref`'s head: {"rows", "snapshot_id", "snapshots", "absent"}.

    Each row is {table, topic, partition, offset, op, lsn, pk} with ints for the numbers. A table
    the sink has not created yet, or one with no `ref`, is `absent` with no rows; any other error
    propagates, so a caller can call the table unreadable.
    """
    from pyiceberg.exceptions import NoSuchTableError

    empty: dict[str, Any] = {"rows": [], "snapshot_id": None, "snapshots": 0, "absent": True}
    try:
        iceberg_table = load_bronze(table)
    except NoSuchTableError:
        return empty
    found = iceberg_table.refs()
    if ref not in found:
        return {**empty, "snapshots": len(iceberg_table.snapshots())}
    snapshot_id = int(found[ref].snapshot_id)
    arrow = iceberg_table.scan(snapshot_id=snapshot_id, selected_fields=ENVELOPE_COLUMNS).to_arrow()
    rows = []
    for row in arrow.to_pylist():
        source = row.get("source")
        topic, partition, offset = offset_key(row)
        rows.append(
            {
                "table": table,
                "topic": topic,
                "partition": partition,
                "offset": offset,
                "op": row["op"],
                "lsn": int(source["lsn"]) if isinstance(source, Mapping) else None,
                "pk": pk_of(table, row.get("after"), row.get("before")),
            }
        )
    return {
        "rows": rows,
        "snapshot_id": snapshot_id,
        "snapshots": len(iceberg_table.snapshots()),
        "absent": False,
    }


# --- postgres side ----------------------------------------------------------------------------


def pg_connect(role: str) -> Any:
    """A psycopg connection to host postgres, database shopstream, as `shopstream` or `cdc`."""
    import psycopg

    if role not in PG_ROLES:
        raise ValueError("role must be shopstream or cdc")
    password = os.environ.get(PG_ROLES[role])
    if not password:
        raise ValueError(f"{PG_ROLES[role]} is not set")
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        dbname="shopstream",
        user=role,
        password=password,
        autocommit=True,
    )


def pg_end_lsn() -> str:
    """pg_current_wal_lsn() as Postgres's own pg_lsn text, taken once the workload has stopped."""
    with pg_connect("cdc") as conn:
        row = conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()
    return str(row[0])


def slot_state(conn: Any, name: str) -> tuple[bool, bool]:
    """(exists, active) for a replication slot."""
    row = conn.execute(
        "SELECT active FROM pg_replication_slots WHERE slot_name = %s", (identifier(name),)
    ).fetchone()
    return (row is not None, bool(row[0]) if row is not None else False)


def slot_create() -> dict[str, str]:
    """Create the test_decoding count slot unless it exists; report created or exists."""
    with pg_connect("cdc") as conn:
        exists, _ = slot_state(conn, COUNT_SLOT)
        if not exists:
            conn.execute(
                "SELECT pg_create_logical_replication_slot(%s, 'test_decoding')", (COUNT_SLOT,)
            )
    return {"slot": COUNT_SLOT, "status": "exists" if exists else "created"}


def slot_drop(names: Sequence[str], wait_s: float = 0.0) -> list[str]:
    """Drop the named slots that exist and return their names; an active slot is refused, named.

    A slot a killed container held can stay active for a moment, so an active slot is re-checked
    for up to `wait_s` seconds before it is refused.
    """
    dropped: list[str] = []
    with pg_connect("cdc") as conn:
        for name in names:
            deadline = time.monotonic() + wait_s
            while True:
                exists, active = slot_state(conn, name)
                if not exists or not active or time.monotonic() >= deadline:
                    break
                time.sleep(2.0)
            if not exists:
                continue
            if active:
                raise RuntimeError(f"slot {name} is active; stop its consumer first")
            conn.execute("SELECT pg_drop_replication_slot(%s)", (name,))
            dropped.append(name)
    return dropped


def pg_changes(end_lsn: str, final: bool = False) -> dict[str, Any]:
    """Count the five tables' changes the count slot decodes up to `end_lsn` (peeked, not consumed).

    With `final` the changes are also read with get_changes to the same LSN, which consumes them,
    and `get_total` reports that second count. The lines stay in memory; only counts are returned.
    """
    sql = "SELECT data FROM {fn}(%s, %s::pg_lsn, NULL)"
    with pg_connect("cdc") as conn:
        peeked = count_changes(
            row[0]
            for row in conn.execute(
                sql.format(fn="pg_logical_slot_peek_changes"), (COUNT_SLOT, end_lsn)
            )
        )
        got = None
        if final:
            got = count_changes(
                row[0]
                for row in conn.execute(
                    sql.format(fn="pg_logical_slot_get_changes"), (COUNT_SLOT, end_lsn)
                )
            )
    return {**peeked, "get_total": got["total"] if got is not None else None}


# --- kafka side -------------------------------------------------------------------------------


def read_committed(
    topics: Sequence[str],
    settle_s: float = SETTLE_S,
    timeout_s: float = 600.0,
    keep_values: bool = False,
) -> tuple[list[tuple[str, int, int, bytes | None]], dict[tuple[str, int], int]]:
    """Every record a read_committed consumer sees below each partition's end: (records, ends).

    A record is (topic, partition, offset, value); a tombstone's value is None, and a non-null
    value is its bytes only with `keep_values` (else b""). The end offsets are read_committed's,
    exclusive, polled until two reads `settle_s` apart are equal. A fresh consumer group that never
    commits reads each partition from its log start to that end, so a second call on the same state
    gives the same records; a record at or beyond the end is ignored.
    """
    from confluent_kafka import Consumer, IsolationLevel, KafkaError, KafkaException, TopicPartition
    from confluent_kafka.admin import AdminClient, OffsetSpec

    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
    partitions = []
    for name in topics:
        meta = admin.list_topics(name, timeout=30).topics[name]
        partitions += [TopicPartition(name, number) for number in sorted(meta.partitions)]

    def read(spec: Any) -> dict[tuple[str, int], int]:
        futures = admin.list_offsets(
            {tp: spec for tp in partitions},
            isolation_level=IsolationLevel.READ_COMMITTED,
            request_timeout=30,
        )
        return {(tp.topic, tp.partition): int(f.result().offset) for tp, f in futures.items()}

    deadline = time.monotonic() + timeout_s
    ends = read(OffsetSpec.latest())
    while True:
        time.sleep(settle_s)
        again = read(OffsetSpec.latest())
        if again == ends:
            break
        ends = again
        if time.monotonic() >= deadline:
            raise TimeoutError("Kafka's end offsets kept moving")
    starts = read(OffsetSpec.earliest())

    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP,
            "group.id": f"cdc-check-{uuid.uuid4()}",
            "enable.auto.commit": False,
            "isolation.level": "read_committed",
            "auto.offset.reset": "earliest",
            "enable.partition.eof": True,
        }
    )
    records: list[tuple[str, int, int, bytes | None]] = []
    done = {key for key, end in ends.items() if starts[key] >= end}
    try:
        consumer.assign([TopicPartition(t, p, starts[(t, p)]) for (t, p) in sorted(ends)])
        while len(done) < len(ends):
            if time.monotonic() >= deadline:
                raise TimeoutError("reading the Kafka set took too long")
            message = consumer.poll(1.0)
            if message is None:
                continue
            error = message.error()
            if error is not None:
                if error.code() == KafkaError._PARTITION_EOF:
                    done.add((message.topic(), message.partition()))
                    continue
                raise KafkaException(error)
            key = (message.topic(), message.partition())
            offset = int(message.offset())
            if offset >= ends[key]:
                done.add(key)
                continue
            value = message.value()
            kept = None if value is None else (bytes(value) if keep_values else b"")
            records.append((key[0], key[1], offset, kept))
            if offset >= ends[key] - 1:
                done.add(key)
    finally:
        consumer.close()
    return records, ends


def kafka_offsets(
    topics: Sequence[str], settle_s: float = SETTLE_S, timeout_s: float = 600.0
) -> tuple[set[Offset], set[Offset], dict[tuple[str, int], int]]:
    """(non-null offsets, tombstone offsets, exclusive end offsets) a read_committed consumer sees."""
    records, ends = read_committed(topics, settle_s, timeout_s)
    nonnull = {(t, p, o) for t, p, o, value in records if value is not None}
    tombstones = {(t, p, o) for t, p, o, value in records if value is None}
    return nonnull, tombstones, ends


def control_events(timeout_s: float = 120.0) -> tuple[list[dict[str, Any]], int]:
    """The decoded head of every control-topic event in topic order, and the undecodable count.

    The control topic is read like any other (read_committed, a fresh group, from its log start).
    A record that does not decode is counted, never raised: a half-written event must not hide the
    others.
    """
    records, _ends = read_committed(
        [connect_admin.CONTROL_TOPIC], settle_s=2.0, timeout_s=timeout_s, keep_values=True
    )
    events: list[dict[str, Any]] = []
    undecodable = 0
    for _topic, _partition, _offset, value in sorted(records, key=lambda r: (r[1], r[2])):
        if not value:
            continue
        try:
            events.append(decode_control_head(value))
        except ValueError:
            undecodable += 1
    return events, undecodable


# --- measurement ------------------------------------------------------------------------------


def gather() -> dict[str, Any]:
    """Read the Kafka set and every bronze table once: the inputs of one measurement."""
    topics = [connect_admin.topic(table) for table in connect_admin.TABLES]
    nonnull, tombstones, ends = kafka_offsets(topics)
    rows: list[dict[str, Any]] = []
    unreadable: list[str] = []
    absent: list[str] = []
    snapshots = 0
    for table in connect_admin.TABLES:
        try:
            found = bronze_rows(table)
        except Exception:
            unreadable.append(table)
            continue
        rows += found["rows"]
        snapshots += int(found["snapshots"])
        if found["absent"]:
            absent.append(table)
    return {
        "nonnull": nonnull,
        "tombstones": tombstones,
        "ends": ends,
        "rows": rows,
        "unreadable": unreadable,
        "absent": absent,
        "snapshots": snapshots,
    }


def summarize(
    data: Mapping[str, Any], pg_total: int, allowed_missing: frozenset[Offset]
) -> dict[str, Any]:
    """The comparison of one `gather()` with the Postgres change count."""
    rows = data["rows"]
    compared = compare_offsets(
        data["nonnull"],
        [(r["topic"], r["partition"], r["offset"]) for r in rows],
        data["ends"],
        allowed_missing,
    )
    stats = change_stats((r["table"], r["pk"], r["lsn"]) for r in rows)
    return {**compared, **stats, "pg_changes": pg_total}


def wait_caught_up(
    timeout_s: float, allowed_missing: frozenset[Offset], pg_total: int
) -> dict[str, Any]:
    """Re-gather every 10 s until bronze holds every Kafka offset and every counted change.

    Caught up means no unreadable table, `missing` within `allowed_missing` and the distinct
    changes at least the Postgres count. On a timeout the last gather is returned, so a real loss
    is reported, not hidden: waiting never changes a count, it only lets the sink and Debezium
    finish.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        data = gather()
        summary = summarize(data, pg_total, allowed_missing)
        caught_up = (
            not data["unreadable"]
            and summary["missing"] <= summary["missing_allowed"]
            and summary["distinct_changes"] >= pg_total
        )
        if caught_up or time.monotonic() >= deadline:
            return data
        time.sleep(CATCH_UP_INTERVAL_S)


def read_kills(stream: Iterable[str]) -> list[dict[str, Any]]:
    """The kill records, one JSON object per line; blank lines and non-JSON lines are skipped."""
    kills = []
    for line in stream:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            record = json.loads(text)
        except ValueError:
            continue
        if isinstance(record, dict):
            kills.append(record)
    return kills


def classify_one(stream: Iterable[str]) -> dict[str, Any]:
    """Classify the first kill record on `stream` from the control topic's events."""
    kills = read_kills(stream)
    if not kills:
        raise ValueError("no kill record on stdin")
    events, undecodable = control_events()
    (kill,) = classify_kills(kills[:1], events)
    return {
        "commit_id": kill.get("commit_id"),
        "classified": kill["classified"],
        "events": kill["events"],
        "undecodable": undecodable,
    }


def versions() -> dict[str, Any]:
    found = connect_admin.plugins()
    return {
        "debezium": found.get("io.debezium.connector.postgresql.PostgresConnector"),
        "iceberg_sink": found.get("org.apache.iceberg.connect.IcebergSinkConnector"),
        "confluent_kafka": package_version("confluent-kafka"),
        "pyiceberg": package_version("pyiceberg"),
        "psycopg": package_version("psycopg"),
    }


def run_item5(kills: list[dict[str, Any]], final: bool, timeout_s: float) -> dict[str, Any]:
    """Take the end LSN, count the changes, wait for catch-up, read both sides and judge."""
    undecodable = 0
    if kills:
        events, undecodable = control_events()
        kills = classify_kills(kills, events)
    end_lsn = pg_end_lsn()
    changes = pg_changes(end_lsn, final=final)
    data = wait_caught_up(timeout_s, frozenset(), int(changes["total"]))
    summary = summarize(data, int(changes["total"]), frozenset())
    rows = data["rows"]
    deletes = delete_stats(
        [(r["table"], r["pk"], r["lsn"]) for r in rows if r["op"] == "d"],
        int(changes["deletes"]),
        [(r["topic"], r["partition"], r["offset"]) for r in rows],
        data["tombstones"],
    )
    counts: dict[str, Any] = {
        **summary,
        "pg_changes_get": changes["get_total"],
        "pg_by_table_op": changes["by_table_op"],
        "tombstones": len(data["tombstones"]),
        "audit_snapshots": data["snapshots"],
        **deletes,
    }
    report: dict[str, Any] = {
        "versions": versions(),
        "end_lsn": end_lsn,
        "end_offsets": {
            f"{topic}:{partition}": end for (topic, partition), end in sorted(data["ends"].items())
        },
        "counts": counts,
        "samples": {
            key: counts.pop(key) for key in ("missing_sample", "extra_sample", "duplicate_sample")
        },
        "delete_example": delete_example(rows, data["tombstones"]),
        "kills": kills,
        "control_undecodable": undecodable,
        "unreadable_tables": data["unreadable"],
        "tables_absent": data["absent"],
    }
    report["verdict"] = item5_verdict(report)
    if final:
        report["slots_dropped"] = slot_drop([COUNT_SLOT])
    return report


def reset() -> dict[str, Any]:
    """The scoped reset of the captured state; Phases 1 to 3's tables and the warehouse stay.

    Drops the Debezium and count slots (a slot a killed Connect held is waited for), truncates the
    five captured tables as their owner, drops item 11's ALTER column so it can run again, purges
    every bronze table through Lakekeeper's REST purge and makes sure the namespace exists.
    """
    dropped = slot_drop([DEBEZIUM_SLOT, COUNT_SLOT], wait_s=60.0)
    tables = ", ".join(identifier(table) for table in PK_COLUMNS)
    with pg_connect("shopstream") as conn:
        conn.execute(f"TRUNCATE {tables}")
        conn.execute("ALTER TABLE customers DROP COLUMN IF EXISTS tier")
    purged = gold_reset.reset(NAMESPACE)
    ensure_namespace()
    return {"slots_dropped": dropped, "truncated": list(PK_COLUMNS), "bronze_purged": purged}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read Debezium envelopes from bronze.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ensure-namespace", help="create the bronze namespace if absent")
    refs_cmd = commands.add_parser("refs", help="print a bronze table's refs")
    refs_cmd.add_argument("--table", required=True, choices=sorted(PK_COLUMNS))
    find = commands.add_parser("bronze-find", help="wait for one row on the audit branch")
    find.add_argument("--table", required=True, choices=sorted(PK_COLUMNS))
    find.add_argument(
        "--pk", required=True, help="primary key value (comma-separated if composite)"
    )
    find.add_argument("--timeout", type=float, default=240.0, help="seconds to wait")
    commands.add_parser("reset", help="scoped reset: slots, captured tables, bronze tables")
    commands.add_parser("slot-create", help="create the test_decoding count slot")
    commands.add_parser("classify", help="classify the kill record on stdin from the control topic")
    drop = commands.add_parser("slot-drop", help="drop the named replication slots")
    drop.add_argument("names", nargs="+")
    item5 = commands.add_parser("item5", help="measure exactly-once and print the verdict")
    item5.add_argument("--kills-stdin", action="store_true", help="read kill records on stdin")
    item5.add_argument("--final", action="store_true", help="also get_changes, then drop the slot")
    item5.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for catch-up")
    return parser


def secrets_in_env() -> list[str]:
    return [value for name in SECRET_ENV if (value := os.environ.get(name))]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "ensure-namespace":
            ensure_namespace()
            print(json.dumps({"namespace": NAMESPACE, "ensured": True}), flush=True)
        elif args.command == "refs":
            print(json.dumps({"table": args.table, "refs": refs(args.table)}), flush=True)
        elif args.command == "bronze-find":
            result = bronze_find(args.table, parse_pk(args.table, args.pk), args.timeout)
            print(json.dumps(result, sort_keys=True), flush=True)
        elif args.command == "reset":
            print(json.dumps(reset(), sort_keys=True), flush=True)
        elif args.command == "slot-create":
            print(json.dumps(slot_create(), sort_keys=True), flush=True)
        elif args.command == "classify":
            print(json.dumps(classify_one(sys.stdin), sort_keys=True), flush=True)
        elif args.command == "slot-drop":
            print(json.dumps({"dropped": slot_drop(args.names)}, sort_keys=True), flush=True)
        else:
            kills = read_kills(sys.stdin) if args.kills_stdin else []
            report = run_item5(kills, args.final, args.timeout)
            print(json.dumps(report, sort_keys=True), flush=True)
            return 1 if report["verdict"]["verdict"] == "inconclusive" else 0
    except Exception as exc:
        print(f"error: {error_text(exc, secrets_in_env())}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
