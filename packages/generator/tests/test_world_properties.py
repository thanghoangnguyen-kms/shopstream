"""ADR-005 P1 to P6 and P8 on the baseline business, each check shown able to fail (PROP-01 to PROP-06).

One `@given` runs a drawn tiny config once and asserts every property on that one run (RESEARCH
Q7). The rest of the file proves the checks have teeth: a small hand-built world that satisfies all
of them, and one change per case that breaks exactly one property and must be reported. A message
names the table, ids and counts, and a last test holds that no message carries a row value
(T-02-19). P7 is `test_fx_snapshot.py`'s.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shopstream_generator import clock
from shopstream_generator.config import ModelConfig
from shopstream_generator.ops import Op, OpKind, Table, Tick
from shopstream_generator.sinks.memory import MemoryCdcSink

from . import harness, properties
from .properties import Key, Row
from .strategies import CANARY, START, tiny_configs

HOUR = clock.US_PER_HOUR
LEAK = "leak-me@example.com"  # a distinctive row value no violation message may carry
LEAK_NAME = "Leaky Person"


# ---------------------------------------------------------------------------------------------
# The property test: one run, every check


@settings(max_examples=60)
@given(config=tiny_configs())
def test_the_baseline_business_holds_p1_to_p6_and_p8(config: ModelConfig) -> None:
    world = harness.run_world(config)
    found = {
        "P1": properties.check_p1(world),
        "P2": properties.check_p2(world),
        "P3": properties.check_p3(world.ticks),
        "P4": properties.check_p4(world, config),
        "P5": properties.check_p5(world.ticks),
        "P6": properties.check_p6(world),
        "P8": properties.check_p8(world),
    }
    # Counts only: a failing example names the property and how many violations, and the messages
    # (table, ids and counts) follow in the assertion's own text.
    assert {name: messages[:3] for name, messages in found.items() if messages} == {}


# ---------------------------------------------------------------------------------------------
# A hand-built world


@dataclass(frozen=True)
class FakeSink:
    """A table view over plain mappings, so a check can be shown a state the real sink refuses."""

    tables: Mapping[Table, Mapping[Key, Row]]

    def table(self, table: Table) -> Mapping[Key, Row]:
        return self.tables.get(table, {})


@dataclass(frozen=True)
class FakeWorld:
    ticks: Sequence[Tick]
    sink: FakeSink
    lines: Mapping[str, Sequence[bytes]]


def dt(us: int) -> datetime:
    return clock.to_datetime(us)


def money(text: str) -> Decimal:
    return Decimal(text)


def customer_row(
    customer_id: int, created: int, updated: int, deleted: int | None = None
) -> dict[str, object]:
    return {
        "customer_id": customer_id,
        "email": LEAK,
        "full_name": LEAK_NAME,
        "city": "Paris",
        "country": "FR",
        "deleted_at": None if deleted is None else dt(deleted),
        "created_at": dt(created),
        "updated_at": dt(updated),
    }


def product_row(
    product_id: int, created: int, updated: int, deleted: int | None = None
) -> dict[str, object]:
    return {
        "product_id": product_id,
        "name": "Widget",
        "category": "tools",
        "list_price": money("10.00"),
        "deleted_at": None if deleted is None else dt(deleted),
        "created_at": dt(created),
        "updated_at": dt(updated),
    }


def order_row(
    order_id: int,
    customer_id: int,
    status: str,
    ordered: int,
    updated: int,
    discount: str = "0.00",
    currency: str = "EUR",
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "status": status,
        "currency_code": currency,
        "order_discount": money(discount),
        "ordered_at": dt(ordered),
        "created_at": dt(ordered),
        "updated_at": dt(updated),
    }


def line_row(
    order_id: int,
    line_number: int,
    product_id: int,
    quantity: int,
    price: str,
    ordered: int,
    updated: int,
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "line_number": line_number,
        "product_id": product_id,
        "quantity": quantity,
        "unit_price": money(price),
        "created_at": dt(ordered),
        "updated_at": dt(updated),
    }


def payment_row(
    payment_id: int, order_id: int, kind: str, amount: str, ts: int, currency: str = "EUR"
) -> dict[str, object]:
    return {
        "payment_id": payment_id,
        "order_id": order_id,
        "payment_kind": kind,
        "amount": money(amount),
        "currency_code": currency,
        "payment_method": "card",
        "paid_at": dt(ts),
        "created_at": dt(ts),
        "updated_at": dt(ts),
    }


def review_row(
    review_id: int, product_id: int, customer_id: int, created: int, updated: int
) -> dict[str, object]:
    return {
        "review_id": review_id,
        "product_id": product_id,
        "customer_id": customer_id,
        "rating": 4,
        "body": "fine",
        "created_at": dt(created),
        "updated_at": dt(updated),
    }


def op(table: Table, kind: OpKind, key: dict[str, int], row: dict[str, object] | None) -> Op:
    return Op(table, kind, key, row)


def insert(table: Table, key: dict[str, int], row: dict[str, object]) -> Op:
    return op(table, OpKind.INSERT, key, row)


def update(table: Table, key: dict[str, int], row: dict[str, object]) -> Op:
    return op(table, OpKind.UPDATE, key, row)


def delete(table: Table, key: dict[str, int]) -> Op:
    return op(table, OpKind.DELETE, key, None)


def scenario() -> list[Tick]:
    """A small business that satisfies every property.

    One customer, two products, an order of two lines with a discount, a line delete that
    recomputes it, payment, shipping and delivery, a refund, a review moderated away, a second
    order placed in USD and cancelled, then the product discontinued and the customer soft-deleted.
    """
    t = START
    c1, p1, p2 = 1, 2, 3
    o1, o2 = 10, 11
    o1_at, o2_at = t + 10, t + 12
    paid, shipped, delivered = t + HOUR, t + 2 * HOUR, t + 3 * HOUR
    groups: list[tuple[int, list[Op]]] = [
        (t + 1, [insert(Table.CUSTOMERS, {"customer_id": c1}, customer_row(c1, t + 1, t + 1))]),
        (t + 2, [insert(Table.PRODUCTS, {"product_id": p1}, product_row(p1, t + 2, t + 2))]),
        (t + 3, [insert(Table.PRODUCTS, {"product_id": p2}, product_row(p2, t + 3, t + 3))]),
        (
            o1_at,
            [
                insert(
                    Table.ORDERS,
                    {"order_id": o1},
                    order_row(o1, c1, "placed", o1_at, o1_at, discount="2.50"),
                ),
                insert(
                    Table.ORDER_ITEMS,
                    {"order_id": o1, "line_number": 1},
                    line_row(o1, 1, p1, 2, "10.00", o1_at, o1_at),
                ),
                insert(
                    Table.ORDER_ITEMS,
                    {"order_id": o1, "line_number": 2},
                    line_row(o1, 2, p2, 1, "5.00", o1_at, o1_at),
                ),
            ],
        ),
        (
            o2_at,
            [
                insert(
                    Table.ORDERS,
                    {"order_id": o2},
                    order_row(o2, c1, "placed", o2_at, o2_at, currency="USD"),
                ),
                insert(
                    Table.ORDER_ITEMS,
                    {"order_id": o2, "line_number": 1},
                    line_row(o2, 1, p1, 1, "11.00", o2_at, o2_at),
                ),
            ],
        ),
        (
            t + 20,
            [
                update(
                    Table.ORDER_ITEMS,
                    {"order_id": o1, "line_number": 2},
                    line_row(o1, 2, p2, 1, "5.00", o1_at, t + 20),
                ),
                delete(Table.ORDER_ITEMS, {"order_id": o1, "line_number": 2}),
                update(
                    Table.ORDERS,
                    {"order_id": o1},
                    order_row(o1, c1, "placed", o1_at, t + 20, discount="2.00"),
                ),
            ],
        ),
        (
            paid,
            [
                update(
                    Table.ORDERS,
                    {"order_id": o1},
                    order_row(o1, c1, "paid", o1_at, paid, discount="2.00"),
                ),
                insert(
                    Table.PAYMENTS,
                    {"payment_id": 100},
                    payment_row(100, o1, "capture", "18.00", paid),
                ),
            ],
        ),
        (
            paid + 1,
            [
                update(
                    Table.ORDERS,
                    {"order_id": o2},
                    order_row(o2, c1, "cancelled", o2_at, paid + 1, currency="USD"),
                )
            ],
        ),
        (
            shipped,
            [
                update(
                    Table.ORDERS,
                    {"order_id": o1},
                    order_row(o1, c1, "shipped", o1_at, shipped, discount="2.00"),
                )
            ],
        ),
        (
            delivered,
            [
                update(
                    Table.ORDERS,
                    {"order_id": o1},
                    order_row(o1, c1, "delivered", o1_at, delivered, discount="2.00"),
                )
            ],
        ),
        (
            delivered + HOUR,
            [
                insert(
                    Table.PAYMENTS,
                    {"payment_id": 101},
                    payment_row(101, o1, "refund", "9.00", delivered + HOUR),
                )
            ],
        ),
        (
            delivered + 2 * HOUR,
            [
                insert(
                    Table.REVIEWS,
                    {"review_id": 7},
                    review_row(7, p1, c1, delivered + 2 * HOUR, delivered + 2 * HOUR),
                )
            ],
        ),
        (
            delivered + 3 * HOUR,
            [
                update(
                    Table.REVIEWS,
                    {"review_id": 7},
                    review_row(7, p1, c1, delivered + 2 * HOUR, delivered + 3 * HOUR),
                ),
                delete(Table.REVIEWS, {"review_id": 7}),
            ],
        ),
        (
            delivered + 4 * HOUR,
            [
                update(
                    Table.PRODUCTS,
                    {"product_id": p2},
                    product_row(p2, t + 3, delivered + 4 * HOUR, delivered + 4 * HOUR),
                )
            ],
        ),
        (
            delivered + 5 * HOUR,
            [
                update(
                    Table.CUSTOMERS,
                    {"customer_id": c1},
                    customer_row(c1, t + 1, delivered + 5 * HOUR, delivered + 5 * HOUR),
                )
            ],
        ),
    ]
    return [Tick(seq, ts, tuple(ops)) for seq, (ts, ops) in enumerate(groups, start=1)]


def scenario_config() -> ModelConfig:
    base = ModelConfig.default(canary_token=CANARY)
    return dataclasses.replace(base, start_us=START, end_us=START + 5 * clock.US_PER_DAY)


def world_of(ticks: Sequence[Tick]) -> FakeWorld:
    """The ticks and a real sink's final state of them, so a good scenario is also valid SQL-wise."""
    sink = MemoryCdcSink()
    for tick in ticks:
        sink.commit(tick)
    tables = {table: dict(sink.table(table)) for table in Table}
    return FakeWorld(ticks, FakeSink(tables), {})


def all_checks(world: FakeWorld) -> dict[str, list[str]]:
    return {
        "P1": properties.check_p1(world),
        "P2": properties.check_p2(world),
        "P3": properties.check_p3(world.ticks),
        "P4": properties.check_p4(world, scenario_config()),
        "P5": properties.check_p5(world.ticks),
        "P6": properties.check_p6(world),
        "P8": properties.check_p8(world),
    }


def tick_index(ticks: Sequence[Tick], table: Table, kind: OpKind, nth: int = 0) -> int:
    """The index of the nth tick holding an op of that table and kind."""
    hits = [
        index
        for index, tick in enumerate(ticks)
        if any(o.table is table and o.kind is kind for o in tick.ops)
    ]
    return hits[nth]


def with_row(o: Op, **columns: object) -> Op:
    """`o` with some after-image columns replaced."""
    assert o.row is not None
    return Op(o.table, o.kind, o.key, {**o.row, **columns})


def edit_op(ticks: Sequence[Tick], tick: int, position: int, new: Op) -> list[Tick]:
    ops = list(ticks[tick].ops)
    ops[position] = new
    out = list(ticks)
    out[tick] = dataclasses.replace(ticks[tick], ops=tuple(ops))
    return out


def edit_row(ticks: Sequence[Tick], tick: int, position: int, **columns: object) -> list[Tick]:
    return edit_op(ticks, tick, position, with_row(ticks[tick].ops[position], **columns))


def append_tick(ticks: Sequence[Tick], ts_after: int, *ops: Op) -> list[Tick]:
    last = ticks[-1]
    return [*ticks, Tick(last.seq + 1, last.ts_us + ts_after, ops)]


def test_the_good_scenario_satisfies_every_property_and_the_sink() -> None:
    world = world_of(scenario())
    assert all_checks(world) == {name: [] for name in all_checks(world)}


# ---------------------------------------------------------------------------------------------
# Teeth: one broken world per check


def broken_p1() -> FakeWorld:
    world = world_of(scenario())
    orders = dict(world.sink.tables[Table.ORDERS])
    row = dict(orders[(10,)])
    row["order_id"] = 99  # the key says 10, the row says 99
    orders[(10,)] = row
    return FakeWorld(world.ticks, FakeSink({**world.sink.tables, Table.ORDERS: orders}), {})


def broken_p1_page_view() -> FakeWorld:
    world = world_of(scenario())
    return FakeWorld(world.ticks, world.sink, {"page_view": [b"x"]})


def broken_p1_null_key() -> FakeWorld:
    world = world_of(scenario())
    items = dict(world.sink.tables[Table.ORDER_ITEMS])
    items[cast(Key, (10, None))] = {"order_id": 10, "line_number": None}
    return FakeWorld(world.ticks, FakeSink({**world.sink.tables, Table.ORDER_ITEMS: items}), {})


def broken_p2_customer() -> FakeWorld:
    ticks = edit_row(scenario(), 3, 0, customer_id=404)
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p2_product() -> FakeWorld:
    ticks = edit_row(scenario(), 3, 1, product_id=404)
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p2_discontinued_product() -> FakeWorld:
    # A line for product 3, placed after the tick that discontinued it.
    ticks = scenario()
    last = ticks[-1].ts_us + 1
    line = line_row(10, 9, 3, 1, "5.00", last, last)
    return FakeWorld(
        append_tick(ticks, 1, insert(Table.ORDER_ITEMS, {"order_id": 10, "line_number": 9}, line)),
        FakeSink({}),
        {},
    )


def broken_p2_payment() -> FakeWorld:
    ticks = edit_row(
        scenario(), tick_index(scenario(), Table.PAYMENTS, OpKind.INSERT), 1, order_id=404
    )
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p2_review() -> FakeWorld:
    # A review by the customer after the soft delete tick.
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    row = review_row(8, 2, 1, ts, ts)
    return FakeWorld(
        append_tick(ticks, 1, insert(Table.REVIEWS, {"review_id": 8}, row)), FakeSink({}), {}
    )


def broken_p2_order_after_delete() -> FakeWorld:
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    row = order_row(12, 1, "placed", ts, ts)
    return FakeWorld(
        append_tick(ticks, 1, insert(Table.ORDERS, {"order_id": 12}, row)), FakeSink({}), {}
    )


def broken_p3_gap() -> FakeWorld:
    ticks = scenario()
    ticks[4] = dataclasses.replace(ticks[4], seq=40)
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p3_not_rising() -> FakeWorld:
    ticks = scenario()
    ticks[2] = dataclasses.replace(ticks[2], ts_us=ticks[1].ts_us)
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p3_updated_at() -> FakeWorld:
    ticks = edit_row(scenario(), 0, 0, updated_at=dt(START))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p3_key_twice() -> FakeWorld:
    ticks = scenario()
    paid = tick_index(ticks, Table.PAYMENTS, OpKind.INSERT)
    again = ticks[paid].ops[0]  # the order update, written a second time in the same tick
    ticks[paid] = dataclasses.replace(ticks[paid], ops=(*ticks[paid].ops, again))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p4_late_touch() -> FakeWorld:
    # An order_items update 100 hours after ordered_at (L is 72 hours).
    ticks = scenario()
    ordered = ticks[3].ts_us
    ts = ordered + 100 * HOUR
    key = {"order_id": 10, "line_number": 1}
    row = line_row(10, 1, 2, 2, "10.00", ordered, ts)
    return FakeWorld(
        append_tick(ticks, ts - ticks[-1].ts_us, update(Table.ORDER_ITEMS, key, row)),
        FakeSink({}),
        {},
    )


def broken_p4_ordered_at() -> FakeWorld:
    ticks = edit_row(scenario(), 3, 0, ordered_at=dt(START))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p4_never_finished() -> FakeWorld:
    ticks = [
        tick
        for tick in scenario()
        if not any(
            o.table is Table.ORDERS and o.row is not None and o.row["status"] == "delivered"
            for o in tick.ops
        )
    ]
    return FakeWorld(
        [dataclasses.replace(tick, seq=seq) for seq, tick in enumerate(ticks, start=1)],
        FakeSink({}),
        {},
    )


def broken_p4_status_after_terminal() -> FakeWorld:
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    row = order_row(10, 1, "shipped", ticks[3].ts_us, ts, discount="2.00")
    return FakeWorld(
        append_tick(ticks, 1, update(Table.ORDERS, {"order_id": 10}, row)), FakeSink({}), {}
    )


def broken_p4_after_cancellation() -> FakeWorld:
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    row = order_row(11, 1, "paid", ticks[4].ts_us, ts, currency="USD")
    return FakeWorld(
        append_tick(ticks, 1, update(Table.ORDERS, {"order_id": 11}, row)), FakeSink({}), {}
    )


def broken_p5_delete_without_update() -> FakeWorld:
    ticks = scenario()
    removal = tick_index(ticks, Table.REVIEWS, OpKind.DELETE)
    ticks[removal] = dataclasses.replace(ticks[removal], ops=(ticks[removal].ops[1],))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p5_delete_on_wrong_table() -> FakeWorld:
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    row = product_row(2, START + 2, ts)
    pair = (
        update(Table.PRODUCTS, {"product_id": 2}, row),
        delete(Table.PRODUCTS, {"product_id": 2}),
    )
    return FakeWorld(append_tick(ticks, 1, *pair), FakeSink({}), {})


def broken_p5_soft_deleted_changes() -> FakeWorld:
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    deleted = ticks[-1].ts_us
    row = customer_row(1, START + 1, ts, deleted)
    return FakeWorld(
        append_tick(ticks, 1, update(Table.CUSTOMERS, {"customer_id": 1}, row)), FakeSink({}), {}
    )


def broken_p5_line_number_reused() -> FakeWorld:
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    ordered = ticks[3].ts_us
    row = line_row(10, 2, 3, 1, "5.00", ordered, ts)  # line 2 of order 10 was deleted earlier
    key = {"order_id": 10, "line_number": 2}
    return FakeWorld(append_tick(ticks, 1, insert(Table.ORDER_ITEMS, key, row)), FakeSink({}), {})


def broken_p5_payment_updated() -> FakeWorld:
    ticks = scenario()
    ts = ticks[-1].ts_us + 1
    row = payment_row(100, 10, "capture", "18.00", ts)
    return FakeWorld(
        append_tick(ticks, 1, update(Table.PAYMENTS, {"payment_id": 100}, row)), FakeSink({}), {}
    )


def broken_p6_discount() -> FakeWorld:
    ticks = edit_row(scenario(), 3, 0, order_discount=money("99.00"))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p6_discount_negative() -> FakeWorld:
    ticks = edit_row(scenario(), 3, 0, order_discount=money("-1.00"))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p6_discount_after_line_delete() -> FakeWorld:
    # The line delete tick leaves the discount at its old value, larger than the lines now left.
    # A discount of the whole gross is fine at placement; the line delete leaves it above the
    # lines still there, and the payment tick repairs it, so only a per-tick check sees it.
    ticks = scenario()
    removal = tick_index(ticks, Table.ORDER_ITEMS, OpKind.DELETE)
    ticks = edit_row(ticks, 3, 0, order_discount=money("25.00"))
    ticks = edit_row(ticks, removal, 2, order_discount=money("25.00"))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p6_refund() -> FakeWorld:
    ticks = scenario()
    refund = tick_index(ticks, Table.PAYMENTS, OpKind.INSERT, 1)
    ticks = edit_row(ticks, refund, 0, amount=money("18.01"))
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p6_currency() -> FakeWorld:
    ticks = scenario()
    capture = tick_index(ticks, Table.PAYMENTS, OpKind.INSERT, 0)
    ticks = edit_row(ticks, capture, 1, currency_code="USD")
    return FakeWorld(ticks, FakeSink({}), {})


def broken_p8() -> FakeWorld:
    world = world_of(scenario())
    orders = dict(world.sink.tables[Table.ORDERS])
    orders[(11,)] = {**orders[(11,)], "currency_code": "BGN"}
    return FakeWorld(world.ticks, FakeSink({**world.sink.tables, Table.ORDERS: orders}), {})


def broken_p8_payment() -> FakeWorld:
    world = world_of(scenario())
    payments = dict(world.sink.tables[Table.PAYMENTS])
    payments[(100,)] = {**payments[(100,)], "currency_code": "XXX"}
    return FakeWorld(world.ticks, FakeSink({**world.sink.tables, Table.PAYMENTS: payments}), {})


TEETH: list[tuple[str, Callable[[], FakeWorld]]] = [
    ("p1", broken_p1),
    ("p1", broken_p1_page_view),
    ("p1", broken_p1_null_key),
    ("p2", broken_p2_customer),
    ("p2", broken_p2_product),
    ("p2", broken_p2_discontinued_product),
    ("p2", broken_p2_payment),
    ("p2", broken_p2_review),
    ("p2", broken_p2_order_after_delete),
    ("p3", broken_p3_gap),
    ("p3", broken_p3_not_rising),
    ("p3", broken_p3_updated_at),
    ("p3", broken_p3_key_twice),
    ("p4", broken_p4_late_touch),
    ("p4", broken_p4_ordered_at),
    ("p4", broken_p4_never_finished),
    ("p4", broken_p4_status_after_terminal),
    ("p4", broken_p4_after_cancellation),
    ("p5", broken_p5_delete_without_update),
    ("p5", broken_p5_delete_on_wrong_table),
    ("p5", broken_p5_soft_deleted_changes),
    ("p5", broken_p5_line_number_reused),
    ("p5", broken_p5_payment_updated),
    ("p6", broken_p6_discount),
    ("p6", broken_p6_discount_negative),
    ("p6", broken_p6_discount_after_line_delete),
    ("p6", broken_p6_refund),
    ("p6", broken_p6_currency),
    ("p8", broken_p8),
    ("p8", broken_p8_payment),
]


@pytest.mark.parametrize(
    ("prop", "build"), TEETH, ids=[f"{prop}-{build.__name__}" for prop, build in TEETH]
)
def test_each_check_reports_a_world_that_breaks_its_property(
    prop: str, build: Callable[[], FakeWorld]
) -> None:
    found = all_checks(build())[prop.upper()]
    assert found, f"{prop} reported nothing for {build.__name__}"
    assert all(message.startswith(prop.upper()) for message in found)


@pytest.mark.parametrize(
    ("prop", "build"), TEETH, ids=[f"{prop}-{build.__name__}" for prop, build in TEETH]
)
def test_violation_messages_hold_no_row_value(prop: str, build: Callable[[], FakeWorld]) -> None:
    messages = [message for found in all_checks(build()).values() for message in found]
    assert messages
    assert not any(LEAK in message or LEAK_NAME in message for message in messages)


def test_the_empty_world_has_the_good_scenarios_shape_and_passes() -> None:
    assert all_checks(FakeWorld([], FakeSink({}), {})) == {
        name: [] for name in ("P1", "P2", "P3", "P4", "P5", "P6", "P8")
    }


# ---------------------------------------------------------------------------------------------
# P1 edge cases


def items_world(*keys: Key) -> FakeWorld:
    rows: dict[Key, Row] = {key: {"order_id": key[0], "line_number": key[1]} for key in keys}
    return FakeWorld([], FakeSink({Table.ORDER_ITEMS: rows}), {})


def test_composite_keys_sharing_an_order_id_or_a_line_number_stay_distinct() -> None:
    world = items_world((1, 1), (1, 2), (2, 1), (2, 2))
    assert properties.check_p1(world) == []


def test_p1_compares_whole_key_tuples() -> None:
    rows: dict[Key, Row] = {
        (1, 2): {"order_id": 1, "line_number": 1}
    }  # the key's line number disagrees
    world = FakeWorld([], FakeSink({Table.ORDER_ITEMS: rows}), {})
    assert properties.check_p1(world)


def test_an_empty_table_passes_p1() -> None:
    assert properties.check_p1(FakeWorld([], FakeSink({}), {})) == []


def test_p1_does_not_depend_on_the_order_rows_are_listed_in() -> None:
    world = world_of(scenario())
    reversed_tables = {
        table: dict(reversed(list(rows.items()))) for table, rows in world.sink.tables.items()
    }
    flipped = FakeWorld(world.ticks, FakeSink(reversed_tables), {})
    assert properties.check_p1(world) == properties.check_p1(flipped) == []
    broken = broken_p1()
    flipped_broken = FakeWorld(
        broken.ticks,
        FakeSink({t: dict(reversed(list(r.items()))) for t, r in broken.sink.tables.items()}),
        {},
    )
    assert sorted(properties.check_p1(broken)) == sorted(properties.check_p1(flipped_broken))


# ---------------------------------------------------------------------------------------------
# P3 on a resumed run


@settings(max_examples=20)
@given(config=tiny_configs(), data=st.data())
def test_p3_holds_on_a_resumed_run(config: ModelConfig, data: st.DataObject) -> None:
    kill_at = config.start_us + data.draw(
        st.integers(0, config.end_us - config.start_us), label="kill_at"
    )
    ticks, _ = harness.run_resumed(config, kill_at)
    assert properties.check_p3(ticks) == []
    if ticks:
        assert [tick.seq for tick in ticks] == list(range(1, len(ticks) + 1))
