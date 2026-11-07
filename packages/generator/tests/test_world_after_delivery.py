"""After delivery: refunds, reviews and moderation (BIZ-05, BIZ-06, BIZ-08, D-16).

All three run at 1_000_000 ppm, so every delivered order that has time left refunds, reviews and
moderates. Each run goes through `harness.run_ticks`, so the Postgres-like sink judges every tick.
The tests read the ticks the engine yields and the engine's own state at the end of a run.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from shopstream_generator import clock, money, textgen
from shopstream_generator.config import BusinessPpm, ModelConfig
from shopstream_generator.engine import Engine, ItemKind
from shopstream_generator.ops import OpKind, Table, Tick
from shopstream_generator.sinks.memory import MemoryCdcSink

from . import harness
from .test_world_orders import as_int, as_str, cents, kinds, order_config, row_of

AFTER_KINDS = frozenset(
    {
        ItemKind.ORDER_STEP,
        ItemKind.LINE_DELETE,
        ItemKind.REFUND,
        ItemKind.REVIEW,
        ItemKind.MODERATION,
    }
)


def after_config(
    *, updates_per_day: int = 60, weights: tuple[int, ...] | None = None, seed: int = 5
) -> ModelConfig:
    """Four busy days with refund, review and moderation certain whenever time allows."""
    base = order_config(days=4, orders_per_day=100, updates_per_day=updates_per_day, seed=seed)
    changed = dataclasses.replace(
        base, business_ppm=BusinessPpm(50_000, 300_000, 1_000_000, 1_000_000, 1_000_000)
    )
    if weights is None:
        return changed
    return dataclasses.replace(changed, update_kind_weights=weights)


@dataclass
class OrderFacts:
    customer_id: int
    currency: str
    lines: dict[int, int]
    delivered_ts: int | None = None
    capture_cents: int | None = None
    method: str | None = None
    refunds: list[tuple[int, int]] = field(default_factory=list)


def facts_of(ticks: list[Tick]) -> dict[int, OrderFacts]:
    """What the order ticks say about each order: parties, surviving lines, capture, refunds."""
    facts: dict[int, OrderFacts] = {}
    for tick in ticks:
        for op in tick.ops:
            row = op.row
            if op.table is Table.ORDERS:
                assert row is not None
                order_id = as_int(row["order_id"])
                if op.kind is OpKind.INSERT:
                    facts[order_id] = OrderFacts(
                        as_int(row["customer_id"]), as_str(row["currency_code"]), {}
                    )
                elif row["status"] == "delivered":
                    facts[order_id].delivered_ts = tick.ts_us
            elif op.table is Table.ORDER_ITEMS:
                order_id = as_int(op.key["order_id"])
                number = as_int(op.key["line_number"])
                if op.kind is OpKind.INSERT:
                    assert row is not None
                    facts[order_id].lines[number] = as_int(row["product_id"])
                elif op.kind is OpKind.DELETE:
                    del facts[order_id].lines[number]
            elif op.table is Table.PAYMENTS:
                assert row is not None
                fact = facts[as_int(row["order_id"])]
                if row["payment_kind"] == "capture":
                    fact.capture_cents = cents(row["amount"])
                    fact.method = as_str(row["payment_method"])
                else:
                    fact.refunds.append((tick.ts_us, cents(row["amount"])))
    return facts


# ---------------------------------------------------------------- refunds


def test_a_refund_is_a_later_payments_insert_within_its_capture() -> None:
    config = after_config()
    percents = [pct for pct, _ in config.payments.refund_percent_weights]
    ticks = harness.run_ticks(config)
    facts = facts_of(ticks)
    refunds = 0
    for tick in ticks:
        for op in tick.ops:
            if op.table is not Table.PAYMENTS or row_of(op)["payment_kind"] != "refund":
                continue
            refunds += 1
            row = row_of(op)
            assert kinds(tick) == [(Table.PAYMENTS, OpKind.INSERT)]
            fact = facts[as_int(row["order_id"])]
            assert fact.delivered_ts is not None
            assert tick.ts_us > fact.delivered_ts, "a refund follows its order's delivery"
            assert fact.capture_cents is not None
            amount = cents(row["amount"])
            assert 1 <= amount <= fact.capture_cents
            assert any(
                amount == money.half_up_div(fact.capture_cents * pct, 100) for pct in percents
            )
            assert row["currency_code"] == fact.currency
            assert row["payment_method"] == fact.method
            assert row["paid_at"] == row["created_at"] == clock.to_datetime(tick.ts_us)
    assert refunds > 50
    assert all(len(fact.refunds) <= 1 for fact in facts.values()), "at most one refund per order"


def test_payment_ids_are_consecutive_across_captures_and_refunds() -> None:
    ids = [
        as_int(row_of(op)["payment_id"])
        for tick in harness.run_ticks(after_config())
        for op in tick.ops
        if op.table is Table.PAYMENTS
    ]
    assert len(ids) > 100
    assert ids == list(range(1, len(ids) + 1))


# ---------------------------------------------------------------- reviews


def test_a_review_follows_a_delivery_for_a_surviving_line_of_a_live_customer() -> None:
    config = after_config()
    ticks = harness.run_ticks(config)
    facts = facts_of(ticks)
    deleted: set[int] = set()
    reviews = 0
    openers = textgen.words("review_openers")
    details = textgen.words("review_details")
    closers = textgen.words("review_closers")
    bodies = {f"{o} {d} {c}" for o in openers for d in details for c in closers}
    for tick in ticks:
        for op in tick.ops:
            row = op.row
            if op.table is Table.CUSTOMERS and row is not None and row["deleted_at"] is not None:
                deleted.add(as_int(row["customer_id"]))
            if op.table is not Table.REVIEWS or op.kind is not OpKind.INSERT:
                continue
            assert row is not None
            reviews += 1
            stamp = clock.to_datetime(tick.ts_us)
            assert kinds(tick) == [(Table.REVIEWS, OpKind.INSERT)]
            assert row["created_at"] == row["updated_at"] == stamp
            customer_id = as_int(row["customer_id"])
            assert customer_id not in deleted, "no review by a soft-deleted customer"
            assert 1 <= as_int(row["rating"]) <= 5
            assert row["body"] in bodies
            product_id = as_int(row["product_id"])
            assert any(
                fact.customer_id == customer_id
                and product_id in fact.lines.values()
                and fact.delivered_ts is not None
                and fact.delivered_ts < tick.ts_us
                for fact in facts.values()
            ), "a review names a delivered order's surviving line of the same customer"
    assert reviews > 50


def test_review_ids_are_consecutive_and_never_reused() -> None:
    ids = [
        as_int(row_of(op)["review_id"])
        for tick in harness.run_ticks(after_config())
        for op in tick.ops
        if op.table is Table.REVIEWS and op.kind is OpKind.INSERT
    ]
    assert len(ids) > 50
    assert ids == list(range(1, len(ids) + 1))


def only_soft_deletes() -> tuple[int, ...]:
    return (0, 0, 0, 1, 0, 0, 0, 0)


def test_no_review_lands_once_every_customer_is_soft_deleted() -> None:
    config = after_config(updates_per_day=600, weights=only_soft_deletes())
    volumes = dataclasses.replace(
        config.volumes, initial_customers=60, customers_per_day=0, orders_per_day=400
    )
    config = dataclasses.replace(config, volumes=volumes)
    ticks = harness.run_ticks(config)
    delivered = [fact for fact in facts_of(ticks).values() if fact.delivered_ts is not None]
    assert len(delivered) > 3, "orders placed before the deletions still run their lifecycle"
    assert any(fact.refunds for fact in delivered), "so their refunds still land"
    tables = [op.table for tick in ticks for op in tick.ops]
    assert Table.REVIEWS not in tables


def test_reviews_do_land_when_nobody_is_soft_deleted() -> None:
    config = after_config(updates_per_day=0)
    tables = [op.table for tick in harness.run_ticks(config) for op in tick.ops]
    assert Table.REVIEWS in tables


# ---------------------------------------------------------------- moderation


def test_moderation_is_an_update_then_delete_pair_of_one_review_in_one_tick() -> None:
    ticks = harness.run_ticks(after_config())
    inserted: dict[int, Mapping[str, object]] = {}
    moderated: set[int] = set()
    for tick in ticks:
        first = tick.ops[0]
        if first.table is not Table.REVIEWS:
            continue
        if first.kind is OpKind.INSERT:
            row = row_of(first)
            inserted[as_int(row["review_id"])] = row
            continue
        assert kinds(tick) == [(Table.REVIEWS, OpKind.UPDATE), (Table.REVIEWS, OpKind.DELETE)]
        review_id = as_int(first.key["review_id"])
        assert tick.ops[1].key == first.key
        assert review_id in inserted, "a review is moderated after its insert"
        assert review_id not in moderated, "a review is moderated at most once"
        moderated.add(review_id)
        stamp = clock.to_datetime(tick.ts_us)
        assert row_of(first) == {**inserted[review_id], "updated_at": stamp}
        created = inserted[review_id]["created_at"]
        assert isinstance(created, datetime)
        assert stamp > created
    assert len(moderated) > 20
    assert set(inserted) >= moderated


# ---------------------------------------------------------------- pruning (C9)


def test_state_keeps_an_order_only_while_a_heap_item_names_it() -> None:
    config = after_config()
    engine = Engine.new(config)
    harness.commit_all(MemoryCdcSink(), engine.run_until(config.end_us))
    named: dict[int, int] = {}
    for _, _, kind, order_id, _ in engine.state.cdc:
        if kind in AFTER_KINDS:
            named[order_id] = named.get(order_id, 0) + 1
    orders = engine.state.world.orders
    assert orders, "the range ends with lifecycles still open"
    assert all(rec.pending > 0 for rec in orders.values())
    assert {order_id: rec.pending for order_id, rec in orders.items()} == named


def test_review_body_joins_one_entry_of_each_list_with_single_spaces() -> None:
    openers = textgen.words("review_openers")
    details = textgen.words("review_details")
    closers = textgen.words("review_closers")
    assert textgen.review_body(0, 0, 0) == f"{openers[0]} {details[0]} {closers[0]}"
    last = textgen.review_body(len(openers) - 1, len(details) - 1, len(closers) - 1)
    assert last == f"{openers[-1]} {details[-1]} {closers[-1]}"
    for name in ("review_openers", "review_details", "review_closers"):
        assert len(textgen.words(name)) >= 15
