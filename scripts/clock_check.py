"""Item 11's analyzer: which field orders CDC changes, and which drives SCD2 validity.

Run inside the `cdc-run` one-shot after `cdc_workload.py --mode seed` and `--mode item11`; the two
workload JSON lines arrive on stdin: `python /app/clock_check.py item11 --workload-stdin --timeout
300`. The last line of stdout is one compact JSON line. Pure rules are tested (`parse_ts_us`,
`parse_sequence`, `commit_ranks`, `order_checks`, `snapshot_check`, `delete_pairs`, `late_products`,
`knob_counts`, `item11_verdict`); the live readers (`read_changes`, `td_lines`, `knobs`) need the
running stack and have no unit tests, like `cdc_check.py`, whose bronze, slot and catch-up readers
this module reuses.

A change is one bronze row of the raw Debezium envelope. Per key (a table and its primary key) the
order is the Kafka offset order within the key's partition. Item 11's questions, each counted rather
than assumed:

- every streamed change (`source.snapshot` is `false`) carries an integer `source.lsn`, and per key
  `source.lsn` rises strictly while its transaction's commit rank never decreases. The rank is the
  position of `COMMIT <xid>` in a `test_decoding` slot's output (created before the run, read with
  include-xids), matched to `source.txId`;
- `after.updated_at` on c, u and r rows and `source.sequence` (a JSON array: the last committed LSN,
  then the current LSN, each possibly null) are checked per key in that order, with ties and
  inversions counted. A null sequence element is counted in `nulls` and compared as -1. Neither
  decides the verdict alone: ADR-001 calls an `updated_at` tie or inversion a generator defect that
  Week 3 fixes;
- snapshot rows (op r) come once per pre-seeded key and before any streamed change of the key;
- every op d row pairs with exactly one op u row of the same key in the same transaction, and its
  `before.updated_at` equals that row's `after.updated_at`.

The CDC-path knob detections item 13 needs are read in the same pass: the Karapace subject's versions
and the optional `tier` column after the ALTER TABLE, the erasure canary counted, late-arriving
products, the seeded review's body hash and the op counts per table. The canary token and every
review body stay in memory: a change carries a flag or a hash, and the report holds counts, synthetic
integer keys, offsets and LSNs, never a row value.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import cdc_check
import connect_admin
from read_v3 import error_text

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
# The values Debezium writes to source.snapshot on a snapshot row; a streamed row carries "false".
SNAPSHOT_FLAGS = frozenset(
    {"true", "first", "first_in_data_collection", "last_in_data_collection", "last"}
)
STREAMED = "false"
TD_COMMIT = re.compile(r"^COMMIT (\d+)")
SUBJECT = "shopstream.public.customers-value"
CANARY_ENV = "CANARY_TOKEN"
FALLBACK_SEQUENCE = "order by source.sequence"
SECRET_ENV = ("SHOPSTREAM_DB_PASSWORD", "CDC_DB_PASSWORD", CANARY_ENV)

Change = Mapping[str, Any]


# --- pure rules -------------------------------------------------------------------------------


def parse_ts_us(value: str | dt.datetime | None) -> int | None:
    """An ISO timestamp (or datetime) as UTC microseconds since the epoch; None stays None.

    Debezium's ZonedTimestamp is an ISO string, so it is parsed, never compared as text. A naive
    value is taken as UTC. Raises ValueError for a string that is not a date.
    """
    if value is None:
        return None
    moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    delta = moment - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def parse_sequence(text: str | None) -> tuple[int | None, int | None]:
    """`source.sequence` as (last committed LSN, current LSN), each an int or None.

    The field is a stringified JSON array of two strings, either of which may be null. Raises
    ValueError for any other shape.
    """
    if text is None:
        return None, None
    parsed = json.loads(text)
    if not isinstance(parsed, list) or len(parsed) != 2:
        raise ValueError("source.sequence is not a two-element JSON array")
    first, second = (None if item is None else int(item) for item in parsed)
    return first, second


def commit_ranks(lines: Iterable[str]) -> dict[int, int]:
    """Each transaction id's commit rank (0, 1, ...) from `test_decoding` lines with include-xids.

    `COMMIT <xid>` lines come in commit order, so the rank is a transaction's position among them;
    a transaction with a BEGIN and no COMMIT has no rank.
    """
    ranks: dict[int, int] = {}
    for line in lines:
        match = TD_COMMIT.match(line)
        if match:
            ranks[int(match.group(1))] = len(ranks)
    return ranks


def is_streamed(item: Change) -> bool:
    return str(item.get("snapshot")).lower() == STREAMED


def _by_key(changes: Iterable[Change]) -> dict[tuple[str, Any], list[Change]]:
    """The changes grouped by (table, primary key), each group in Kafka offset order."""
    groups: dict[tuple[str, Any], list[Change]] = defaultdict(list)
    for item in changes:
        groups[(item["table"], item["pk"])].append(item)
    for group in groups.values():
        group.sort(key=lambda c: (c["partition"], c["offset"]))
    return groups


def _compare(values: Iterable[Any]) -> dict[str, int]:
    """Consecutive comparisons: how many, how many equal (ties) and how many decrease (inversions)."""
    comparisons = ties = inversions = 0
    previous: Any = None
    first = True
    for value in values:
        if not first:
            comparisons += 1
            if value == previous:
                ties += 1
            elif value < previous:
                inversions += 1
        previous, first = value, False
    return {"comparisons": comparisons, "ties": ties, "inversions": inversions}


def order_checks(changes: Iterable[Change], ranks: Mapping[int, int]) -> dict[str, Any]:
    """Per-key order of source.lsn, commit rank, after.updated_at and source.sequence.

    `changes` are dicts {table, pk, partition, offset, op, lsn, txid, snapshot, after_ts, before_ts,
    sequence}. Only streamed changes (source.snapshot "false") enter the LSN, rank and sequence
    checks; c, u and r rows with an after.updated_at enter the updated_at check, and an op d row never
    does. A violation is a decrease or a tie in `lsn` and a decrease in the rank (rows of one
    transaction share a rank). A streamed change with no source.lsn is `missing_lsn`; one whose txid
    has no commit rank is `unranked` and skipped by the rank check.
    """
    result: dict[str, Any] = {
        "streamed": 0,
        "keys_checked": 0,
        "missing_lsn": 0,
        "lsn_violations": 0,
        "rank_violations": 0,
        "unranked": 0,
        "updated_at": {"comparisons": 0, "ties": 0, "inversions": 0},
        "sequence": {"comparisons": 0, "ties": 0, "inversions": 0, "nulls": 0},
    }
    for group in _by_key(changes).values():
        live = [c for c in group if is_streamed(c)]
        if live:
            result["keys_checked"] += 1
        result["streamed"] += len(live)
        previous_lsn: int | None = None
        previous_rank: int | None = None
        for item in live:
            lsn = item["lsn"]
            if lsn is None:
                result["missing_lsn"] += 1
            else:
                if previous_lsn is not None and lsn <= previous_lsn:
                    result["lsn_violations"] += 1
                previous_lsn = lsn
            rank = ranks.get(item["txid"]) if item["txid"] is not None else None
            if rank is None:
                result["unranked"] += 1
            else:
                if previous_rank is not None and rank < previous_rank:
                    result["rank_violations"] += 1
                previous_rank = rank
        stamps = [
            c["after_ts"] for c in group if c["op"] in ("c", "u", "r") and c["after_ts"] is not None
        ]
        for name, value in _compare(stamps).items():
            result["updated_at"][name] += value
        pairs = [parse_sequence(c["sequence"]) for c in live]
        result["sequence"]["nulls"] += sum(element is None for pair in pairs for element in pair)
        for name, value in _compare(
            [tuple(-1 if e is None else e for e in p) for p in pairs]
        ).items():
            result["sequence"][name] += value
    return result


def snapshot_check(changes: Iterable[Change], seeded: Mapping[str, int]) -> dict[str, Any]:
    """Snapshot rows (op r) come exactly one per seeded key and before any streamed change of the key.

    `seeded` maps each table to the number of rows seeded before the connector started. `ok` needs
    every table's r-row count to equal its seeded count with no key repeated, every flag to be one of
    Debezium's snapshot values, and no r row after a streamed change of the same key.
    """
    items = list(changes)
    r_rows = [c for c in items if c["op"] == "r"]
    keys = Counter((c["table"], c["pk"]) for c in r_rows)
    flags = Counter(str(c["snapshot"]).lower() for c in r_rows)
    r_after_streamed = 0
    for group in _by_key(items).values():
        seen_streamed = False
        for item in group:
            if is_streamed(item):
                seen_streamed = True
            elif item["op"] == "r" and seen_streamed:
                r_after_streamed += 1
    per_table = Counter(table for table, _pk in keys)
    counts_match = all(per_table.get(table, 0) == int(n) for table, n in seeded.items()) and set(
        per_table
    ) <= set(seeded)
    one = sum(1 for count in keys.values() if count == 1)
    many = sum(1 for count in keys.values() if count > 1)
    return {
        "seeded_keys": sum(int(n) for n in seeded.values()),
        "r_rows": len(r_rows),
        "keys_with_one_r": one,
        "keys_with_many_r": many,
        "r_after_streamed": r_after_streamed,
        "flags": dict(sorted(flags.items())),
        "ok": counts_match and many == 0 and r_after_streamed == 0 and set(flags) <= SNAPSHOT_FLAGS,
    }


def delete_pairs(changes: Iterable[Change]) -> dict[str, Any]:
    """Every op d row pairs with exactly one op u row of its key and transaction, at the delete time.

    The u row's after.updated_at is the simulated delete time; the d row's before.updated_at must
    equal it (REPLICA IDENTITY FULL). A d row with no such u row, or with several, is `unpaired`.
    """
    updates: dict[tuple[str, Any, Any], list[Change]] = defaultdict(list)
    deletes: list[Change] = []
    for item in changes:
        if not is_streamed(item):
            continue
        if item["op"] == "u":
            updates[(item["table"], item["pk"], item["txid"])].append(item)
        elif item["op"] == "d":
            deletes.append(item)
    paired = unpaired = mismatch = 0
    for deleted in deletes:
        found = updates.get((deleted["table"], deleted["pk"], deleted["txid"]), [])
        if len(found) != 1:
            unpaired += 1
            continue
        paired += 1
        if found[0]["after_ts"] is None or deleted["before_ts"] != found[0]["after_ts"]:
            mismatch += 1
    return {
        "d_rows": len(deletes),
        "paired": paired,
        "unpaired": unpaired,
        "time_mismatch": mismatch,
        "ok": unpaired == 0 and mismatch == 0,
    }


def late_products(
    order_item_changes: Iterable[Change],
    product_changes: Iterable[Change],
    product_ids: Sequence[int],
) -> dict[str, int]:
    """Late-arriving products: an order_items row whose source.lsn is below its product's insert's.

    `order_item_changes` carry `product_id`; `product_changes` are the products' insert rows, keyed by
    `pk`. `missing_product` counts expected products with no insert row in bronze.
    """
    first_item: dict[int, int] = {}
    for item in order_item_changes:
        product, lsn = item.get("product_id"), item["lsn"]
        if product is not None and lsn is not None and lsn < first_item.get(product, lsn + 1):
            first_item[product] = lsn
    inserted = {c["pk"][0]: c["lsn"] for c in product_changes if c["pk"] is not None}
    found = missing = 0
    for product in product_ids:
        if product not in inserted:
            missing += 1
        elif (
            product in first_item
            and inserted[product] is not None
            and first_item[product] < inserted[product]
        ):
            found += 1
    return {"expected": len(product_ids), "found": found, "missing_product": missing}


def knob_counts(changes: Iterable[Change], workload: Mapping[str, Any]) -> dict[str, Any]:
    """The knob detections that need only the changes: canary, late products, review and op counts."""
    items = list(changes)
    review = workload.get("review") or {}
    review_rows = [
        c
        for c in items
        if c["table"] == "reviews"
        and c["op"] == "c"
        and c["pk"] == (review.get("review_id"),)
        and review.get("review_id") is not None
    ]
    ops: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        ops[item["table"]][item["op"]] += 1
    product_ids = [int(p) for p in (workload.get("late_products") or {}).get("product_ids", [])]
    return {
        "canary": {"rows": sum(1 for c in items if c.get("canary"))},
        "late_products": late_products(
            [c for c in items if c["table"] == "order_items" and c["op"] == "c"],
            [c for c in items if c["table"] == "products" and c["op"] == "c"],
            product_ids,
        ),
        "review": {
            "rows": len(review_rows),
            "body_sha256_matches": bool(review_rows)
            and all(c.get("body_sha256") == review.get("body_sha256") for c in review_rows),
        },
        "ops": {table: dict(sorted(counter.items())) for table, counter in sorted(ops.items())},
    }


def item11_verdict(report: Mapping[str, Any]) -> dict[str, Any]:
    """Item 11's rule: go, fallback (order by source.sequence) or inconclusive.

    Inconclusive when nothing streamed, a streamed change has no commit rank, the snapshot check
    fails, a delete does not pair or bronze had not caught up. Otherwise go when no streamed change
    lacks source.lsn and per-key source.lsn and commit-rank order both hold; fallback only when that
    fails while source.sequence rises strictly per key with no null; else inconclusive. An updated_at
    tie or inversion never decides: ADR-001 calls it a generator defect Week 3 fixes.
    """
    order, snapshot, deletes = report["order"], report["snapshot"], report["deletes"]
    problems: list[str] = []
    if order["streamed"] == 0:
        problems.append("no streamed change was found in bronze, so there is nothing to order")
    if order["unranked"]:
        problems.append(
            f"{order['unranked']} streamed change(s) have no commit rank in the test_decoding output"
        )
    if not snapshot["ok"]:
        problems.append(
            f"the snapshot check failed: {snapshot['r_rows']} op r rows for {snapshot['seeded_keys']} "
            f"seeded keys, {snapshot['keys_with_many_r']} repeated, "
            f"{snapshot['r_after_streamed']} after a streamed change"
        )
    if not deletes["ok"]:
        problems.append(
            f"{deletes['unpaired']} delete(s) do not pair with one update of their transaction and "
            f"{deletes['time_mismatch']} pair(s) differ in time"
        )
    if report.get("caught_up") is False:
        problems.append("bronze had not caught up with Kafka and test_decoding before the analysis")
    if problems:
        return {"verdict": "inconclusive", "fallback": None, "reasons": problems}
    lsn_ok = (
        order["missing_lsn"] == 0 and order["lsn_violations"] == 0 and order["rank_violations"] == 0
    )
    if lsn_ok:
        return {
            "verdict": "go",
            "fallback": None,
            "reasons": [
                f"all {order['streamed']} streamed changes carry source.lsn and rise strictly per key "
                f"in commit order across {order['keys_checked']} keys"
            ],
        }
    findings = (
        f"source.lsn order fails: {order['missing_lsn']} change(s) without it, "
        f"{order['lsn_violations']} per-key violation(s), {order['rank_violations']} commit-rank "
        f"violation(s)"
    )
    sequence = order["sequence"]
    if sequence["ties"] == 0 and sequence["inversions"] == 0 and sequence["nulls"] == 0:
        return {
            "verdict": "fallback",
            "fallback": FALLBACK_SEQUENCE,
            "reasons": [findings, "source.sequence rises strictly per key with no null"],
        }
    return {
        "verdict": "inconclusive",
        "fallback": None,
        "reasons": [
            findings,
            f"source.sequence does not rescue it: {sequence['ties']} tie(s), "
            f"{sequence['inversions']} inversion(s), {sequence['nulls']} null element(s)",
        ],
    }


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


def change_from_row(
    table: str, row: Mapping[str, Any], canary: str | None = None
) -> dict[str, Any]:
    """One bronze row of the Debezium envelope as a change dict.

    The canary token is compared here, in memory, and only a flag leaves; a review body leaves as its
    sha256; no email, name or body is kept.
    """
    raw_after, raw_before, raw_source = row.get("after"), row.get("before"), row.get("source")
    after = raw_after if isinstance(raw_after, Mapping) else None
    before = raw_before if isinstance(raw_before, Mapping) else None
    source: Mapping[str, Any] = raw_source if isinstance(raw_source, Mapping) else {}
    snapshot = source.get("snapshot")
    body = after.get("body") if after is not None and table == "reviews" else None
    names = [image.get("full_name") for image in (after, before) if image is not None]
    return {
        "table": table,
        "pk": cdc_check.pk_of(table, after, before),
        "partition": int(row["_kafka_metadata_partition"]),
        "offset": int(row["_kafka_metadata_offset"]),
        "op": row["op"],
        "lsn": _int(source.get("lsn")),
        "txid": _int(source.get("txId", source.get("txid"))),
        "snapshot": None if snapshot is None else str(snapshot).lower(),
        "after_ts": parse_ts_us(after.get("updated_at")) if after is not None else None,
        "before_ts": parse_ts_us(before.get("updated_at")) if before is not None else None,
        "sequence": source.get("sequence"),
        "product_id": _int(after.get("product_id"))
        if after is not None and table == "order_items"
        else None,
        "tier_set": after is not None and after.get("tier") is not None,
        "canary": bool(canary) and table == "customers" and canary in names,
        "body_sha256": None if body is None else hashlib.sha256(str(body).encode()).hexdigest(),
    }


def read_workload(lines: Iterable[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The seed and item11 workload reports from stdin lines; other lines are skipped.

    When a mode appears more than once the last report wins. Raises ValueError when either is absent.
    """
    found: dict[str, dict[str, Any]] = {}
    for line in lines:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            record = json.loads(text)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("mode") in ("seed", "item11"):
            found[str(record["mode"])] = record
    missing = [mode for mode in ("seed", "item11") if mode not in found]
    if missing:
        raise ValueError(f"stdin has no workload report for: {', '.join(missing)}")
    return found["seed"], found["item11"]


def analyze(
    changes: Sequence[Change], ranks: Mapping[int, int], seeded: Mapping[str, int]
) -> dict[str, Any]:
    """The ordering, snapshot and delete reports over one set of changes, with the verdict."""
    report = {
        "order": order_checks(changes, ranks),
        "snapshot": snapshot_check(changes, seeded),
        "deletes": delete_pairs(changes),
    }
    return {"report": report, "verdict": item11_verdict(report)}


# --- live readers -----------------------------------------------------------------------------


def read_changes(canary: str | None) -> dict[str, Any]:
    """Every bronze change at each table's audit head: {"changes", "snapshots", "absent"}.

    The audit head is fixed per table at the time of the read, so a rerun on the same stopped state
    reads the same rows. A table the sink has not created, or without an audit branch, is `absent`.
    """
    from pyiceberg.exceptions import NoSuchTableError

    out: list[dict[str, Any]] = []
    snapshots: dict[str, int] = {}
    absent: list[str] = []
    for table in connect_admin.TABLES:
        try:
            iceberg_table = cdc_check.load_bronze(table)
        except NoSuchTableError:
            absent.append(table)
            continue
        found = iceberg_table.refs()
        if cdc_check.AUDIT_BRANCH not in found:
            absent.append(table)
            continue
        snapshot_id = int(found[cdc_check.AUDIT_BRANCH].snapshot_id)
        snapshots[table] = snapshot_id
        arrow = iceberg_table.scan(
            snapshot_id=snapshot_id, selected_fields=cdc_check.ENVELOPE_COLUMNS
        ).to_arrow()
        out += [change_from_row(table, row, canary) for row in arrow.to_pylist()]
    return {"changes": out, "snapshots": snapshots, "absent": absent}


def td_lines(end_lsn: str) -> list[str]:
    """The count slot's decoded lines up to `end_lsn`, with transaction ids, peeked not consumed.

    The lines hold row values (the canary among them), so they stay in memory: callers take the
    commit ranks and the change counts and never print a line.
    """
    with cdc_check.pg_connect("cdc") as conn:
        rows = conn.execute(
            "SELECT data FROM pg_logical_slot_peek_changes(%s, %s::pg_lsn, NULL, "
            "'include-xids', '1', 'include-timestamp', '1')",
            (cdc_check.COUNT_SLOT, end_lsn),
        ).fetchall()
    return [str(row[0]) for row in rows]


def alter_facts(changes: Sequence[Change]) -> dict[str, Any]:
    """Karapace's subject versions and the optional `tier` column in bronze after the ALTER TABLE."""
    status, payload = connect_admin.http_request(
        "GET", f"{connect_admin.KARAPACE_URL}/subjects/{SUBJECT}/versions", {}, None
    )
    versions: list[int] = []
    if status == 200:
        parsed = json.loads(payload)
        versions = [int(v) for v in parsed] if isinstance(parsed, list) else []
    after = cdc_check.load_bronze("customers").schema().find_field("after")
    names = [field.name for field in getattr(after.field_type, "fields", ())]
    return {
        "subject_http": status,
        "subject_versions": versions,
        "bronze_has_tier": "tier" in names,
        "rows_with_tier": sum(
            1 for c in changes if c["table"] == "customers" and c.get("tier_set")
        ),
    }


def knobs(changes: Sequence[Change], workload: Mapping[str, Any]) -> dict[str, Any]:
    """The CDC-path knob detections of the run: `alter`, `canary`, `late_products`, `review`, `ops`."""
    counts = knob_counts(changes, workload)
    return {"alter": alter_facts(changes), **counts}


def run_item11(seed: Mapping[str, Any], run: Mapping[str, Any], timeout_s: float) -> dict[str, Any]:
    """Take the end LSN, rank the commits, wait for catch-up, read bronze and judge."""
    canary = os.environ.get(CANARY_ENV) or None
    end_lsn = str(run.get("end_lsn") or cdc_check.pg_end_lsn())
    lines = td_lines(end_lsn)
    ranks = commit_ranks(lines)
    totals = cdc_check.count_changes(lines)
    seeded = {table: int(n) for table, n in (seed.get("seeded_keys") or {}).items()}
    expected = int(totals["total"]) + sum(seeded.values())
    data = cdc_check.wait_caught_up(timeout_s, frozenset(), expected)
    summary = cdc_check.summarize(data, expected, frozenset())
    caught_up = (
        not data["unreadable"]
        and summary["missing"] <= summary["missing_allowed"]
        and summary["distinct_changes"] >= expected
    )
    read = read_changes(canary)
    changes: list[dict[str, Any]] = read["changes"]
    analysis = analyze(changes, ranks, seeded)
    verdict = item11_verdict({**analysis["report"], "caught_up": caught_up})
    return {
        "versions": cdc_check.versions(),
        "end_lsn": end_lsn,
        "snapshots": read["snapshots"],
        "tables_absent": read["absent"],
        "td_changes": totals["total"],
        "td_by_table_op": totals["by_table_op"],
        "commits_ranked": len(ranks),
        "catch_up": {
            "caught_up": caught_up,
            "expected_distinct": expected,
            "distinct_changes": summary["distinct_changes"],
            "missing": summary["missing"],
        },
        "report": {key: analysis["report"][key] for key in ("order", "snapshot", "deletes")},
        "knobs": knobs(changes, run),
        "verdict": verdict,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Item 11's two clocks, measured in bronze.")
    commands = parser.add_subparsers(dest="command", required=True)
    item11 = commands.add_parser("item11", help="analyze a finished seed and item11 workload run")
    item11.add_argument(
        "--workload-stdin", action="store_true", required=True, help="read the workload JSON lines"
    )
    item11.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for catch-up")
    return parser


def secrets_in_env() -> list[str]:
    return [value for name in SECRET_ENV if (value := os.environ.get(name))]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        seed, run = read_workload(sys.stdin)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        report = run_item11(seed, run, args.timeout)
    except Exception as exc:
        print(f"error: {error_text(exc, secrets_in_env())}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True), flush=True)
    return 1 if report["verdict"]["verdict"] == "inconclusive" else 0


if __name__ == "__main__":
    sys.exit(main())
