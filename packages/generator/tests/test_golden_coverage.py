"""The golden's 7 days exercise every baseline path (HASH-02, D-22, RESEARCH Q9).

A golden that never places a refund or a moderation delete would match itself forever and prove
nothing about them. This test runs the golden config in-process, classifies each op by comparing it
with the key's previous row, and asserts every path occurs at least once. It prints counts only,
never a row: public CI logs are copies erasure can't reach (ADR-005).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from shopstream_generator import clock, config
from shopstream_generator.ops import PAGE_VIEW, STREAMS, Op, OpKind, Table

from . import harness

GOLDEN_CONFIG = Path(__file__).resolve().parent / "golden" / "config.json"
# Phase 3's drift, canary and injection instants all sit at this moment.
DRIFT_INSTANT_US = clock.parse("2025-07-01T00:00:00.000000Z")
EURO = "EUR"

PATHS = (
    "orders_placed",
    "orders_paid",
    "orders_shipped",
    "orders_delivered",
    "orders_cancelled",
    "captures",
    "refunds",
    "reviews",
    "moderation_deletes",
    "line_deletes",
    "customer_moves",
    "customer_email_edits",
    "customer_name_edits",
    "customer_soft_deletes",
    "product_name_edits",
    "product_category_edits",
    "product_price_edits",
    "product_discontinuations",
    "non_eur_orders",
)


def changed(before: Mapping[str, object], after: Mapping[str, object], *columns: str) -> bool:
    return any(before[column] != after[column] for column in columns)


def classify(op: Op, before: Mapping[str, object] | None, counts: Counter[str]) -> None:
    """Add one op's paths to `counts`; `before` is the key's previous row, if any."""
    row = op.row
    if op.table is Table.ORDERS and row is not None:
        if op.kind is OpKind.INSERT:
            counts["orders_placed"] += 1
            counts["non_eur_orders"] += row["currency_code"] != EURO
        elif before is not None and before["status"] != row["status"]:
            counts[f"orders_{row['status']}"] += 1
    elif op.table is Table.PAYMENTS and row is not None:
        counts[f"{row['payment_kind']}s"] += 1
    elif op.table is Table.REVIEWS:
        counts["reviews"] += op.kind is OpKind.INSERT
        counts["moderation_deletes"] += op.kind is OpKind.DELETE
    elif op.table is Table.ORDER_ITEMS:
        counts["line_deletes"] += op.kind is OpKind.DELETE
    elif op.table is Table.CUSTOMERS and op.kind is OpKind.UPDATE and row and before:
        counts["customer_moves"] += changed(before, row, "city", "country")
        counts["customer_email_edits"] += changed(before, row, "email")
        counts["customer_name_edits"] += changed(before, row, "full_name")
        counts["customer_soft_deletes"] += changed(before, row, "deleted_at")
    elif op.table is Table.PRODUCTS and op.kind is OpKind.UPDATE and row and before:
        counts["product_name_edits"] += changed(before, row, "name")
        counts["product_category_edits"] += changed(before, row, "category")
        counts["product_price_edits"] += changed(before, row, "list_price")
        counts["product_discontinuations"] += changed(before, row, "deleted_at")


def test_the_golden_covers_every_path() -> None:
    golden = config.load(GOLDEN_CONFIG)
    world = harness.run_world(golden)
    counts: Counter[str] = Counter()
    rows: dict[tuple[Table, tuple[int, ...]], Mapping[str, object]] = {}
    for tick in world.ticks:
        for op in tick.ops:
            key = (op.table, tuple(op.key.values()))
            classify(op, rows.get(key), counts)
            if op.row is not None:
                rows[key] = op.row
    # Every captured stream but the clickstream has rows; the clickstream starts in Phase 3.
    streams = {name: len(world.lines[name]) for name in STREAMS}
    counts_only = {name: counts[name] for name in PATHS}
    print("golden paths", counts_only)
    print("golden streams", streams)

    assert [name for name, count in counts_only.items() if count < 1] == []
    assert [name for name, count in streams.items() if count == 0 and name != PAGE_VIEW] == []
    assert streams[PAGE_VIEW] == 0
    assert golden.start_us <= DRIFT_INSTANT_US < golden.end_us
