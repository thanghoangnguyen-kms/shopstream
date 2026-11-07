"""Orders through the capture: placement, pricing, discount, lifecycle, line deletes (BIZ-03 to BIZ-05, BIZ-07, BIZ-08).

Every run goes through `harness.run_ticks`, so each tick is also judged by the Postgres-like sink.
The tests read only the ticks the engine yields, replaying the customers and products ticks to
know who was live and what a list price was at the instant an order was placed.

The business rates for refunds, reviews and moderation are zero here: this file is about the
order itself, and its assertions must hold whatever the later processes add after delivery.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from shopstream_generator import canon, clock, countries, fx, money
from shopstream_generator.config import PAYMENT_METHODS, BusinessPpm, ModelConfig
from shopstream_generator.engine import Engine
from shopstream_generator.money import CENT, MONEY
from shopstream_generator.ops import Op, OpKind, Table, Tick
from shopstream_generator.rng import StreamName
from shopstream_generator.sinks.memory import MemoryCdcSink

from . import harness
from .strategies import CANARY, START

PAID_PATH = ("placed", "paid", "shipped", "delivered")
CANCELLED_PATH = ("placed", "cancelled")


def order_config(
    *,
    cancel_ppm: int = 100_000,
    line_delete_ppm: int = 300_000,
    orders_per_day: int = 200,
    days: int = 2,
    seed: int = 7,
    initial_customers: int = 60,
    initial_products: int = 60,
    customers_per_day: int = 20,
    products_per_day: int = 2,
    updates_per_day: int = 100,
) -> ModelConfig:
    """A busy two-day config; refund, review and moderation are off so orders stand alone."""
    base = ModelConfig.default(canary_token=CANARY)
    volumes = dataclasses.replace(
        base.volumes,
        initial_customers=initial_customers,
        initial_products=initial_products,
        customers_per_day=customers_per_day,
        products_per_day=products_per_day,
        orders_per_day=orders_per_day,
        updates_per_day=updates_per_day,
    )
    return dataclasses.replace(
        base,
        seed=seed,
        start_us=START,
        end_us=START + days * clock.US_PER_DAY,
        volumes=volumes,
        business_ppm=BusinessPpm(cancel_ppm, line_delete_ppm, 0, 0, 0),
    )


# ---------------------------------------------------------------- typed readers over rows


def row_of(op: Op) -> Mapping[str, object]:
    assert op.row is not None
    return op.row


def as_int(value: object) -> int:
    assert type(value) is int
    return value


def as_str(value: object) -> str:
    assert isinstance(value, str)
    return value


def as_money(value: object) -> Decimal:
    assert isinstance(value, Decimal)
    assert value.as_tuple().exponent == -2
    return value


def cents(value: object) -> int:
    """A NUMERIC(18,2) value as integer cents."""
    return int(MONEY.scaleb(as_money(value), 2))


def is_placement(tick: Tick) -> bool:
    first = tick.ops[0]
    return first.table is Table.ORDERS and first.kind is OpKind.INSERT


def kinds(tick: Tick) -> list[tuple[Table, OpKind]]:
    return [(op.table, op.kind) for op in tick.ops]


@dataclass
class Parties:
    """Who is live and what things cost, replayed from the customers and products ticks."""

    country: dict[int, str] = field(default_factory=dict)
    deleted_customers: set[int] = field(default_factory=set)
    price: dict[int, Decimal] = field(default_factory=dict)
    discontinued: set[int] = field(default_factory=set)

    def apply(self, tick: Tick) -> None:
        for op in tick.ops:
            if op.table is Table.CUSTOMERS:
                row = row_of(op)
                customer_id = as_int(row["customer_id"])
                self.country[customer_id] = as_str(row["country"])
                if row["deleted_at"] is not None:
                    self.deleted_customers.add(customer_id)
            elif op.table is Table.PRODUCTS:
                row = row_of(op)
                product_id = as_int(row["product_id"])
                self.price[product_id] = as_money(row["list_price"])
                if row["deleted_at"] is not None:
                    self.discontinued.add(product_id)


def gross_of(lines: Mapping[int, Mapping[str, object]]) -> int:
    return sum(as_int(line["quantity"]) * cents(line["unit_price"]) for line in lines.values())


# ---------------------------------------------------------------- placement


def test_a_placement_tick_holds_one_header_then_numbered_lines_for_live_parties() -> None:
    config = order_config()
    quotes = fx.latest_quotes()
    currency_of = {country.code: country.currency for country in countries.countries()}
    parties = Parties()
    seen_currencies: set[str] = set()
    placements = 0
    for tick in harness.run_ticks(config):
        if not is_placement(tick):
            parties.apply(tick)
            continue
        placements += 1
        header = row_of(tick.ops[0])
        stamp = clock.to_datetime(tick.ts_us)
        assert header["status"] == "placed"
        assert header["ordered_at"] == header["created_at"] == header["updated_at"] == stamp
        customer_id = as_int(header["customer_id"])
        assert customer_id in parties.country, "an order names a landed customer"
        assert customer_id not in parties.deleted_customers, "and one that isn't soft-deleted"
        code = as_str(header["currency_code"])
        assert code == currency_of[parties.country[customer_id]], "the currency follows the country"
        seen_currencies.add(code)
        lines = tick.ops[1:]
        assert 1 <= len(lines) <= config.orders.max_lines
        gross = 0
        for number, op in enumerate(lines, start=1):
            assert (op.table, op.kind) == (Table.ORDER_ITEMS, OpKind.INSERT)
            line = row_of(op)
            assert line["order_id"] == header["order_id"]
            assert line["line_number"] == number
            assert line["created_at"] == line["updated_at"] == stamp
            product_id = as_int(line["product_id"])
            assert product_id in parties.price, "a line names a landed product"
            assert product_id not in parties.discontinued, "and one that isn't discontinued"
            list_price = parties.price[product_id]
            if code == "EUR":
                expected = list_price
            else:
                expected = MONEY.quantize(MONEY.multiply(list_price, quotes[code]), CENT)
            assert line["unit_price"] == expected
            quantity = as_int(line["quantity"])
            assert 1 <= quantity <= config.orders.max_quantity
            gross += quantity * cents(line["unit_price"])
        discount = cents(header["order_discount"])
        percents = [pct for pct, _ in config.orders.discount_percent_weights]
        assert any(money.half_up_div(gross * pct, 100) == discount for pct in percents)
        assert 0 <= discount <= gross
    assert placements > 100
    assert "EUR" in seen_currencies
    assert len(seen_currencies) > 3


def test_every_order_id_is_placed_once_and_ids_are_consecutive() -> None:
    ids = [
        as_int(row_of(tick.ops[0])["order_id"])
        for tick in harness.run_ticks(order_config())
        if is_placement(tick)
    ]
    assert len(ids) > 100
    assert ids == list(range(1, len(ids) + 1))


# ---------------------------------------------------------------- the lifecycle


def test_each_order_follows_one_of_two_paths_within_48_hours_of_its_insert() -> None:
    config = order_config()
    budget = config.horizons.edit_horizon - config.horizons.late_order_max
    assert budget == 48 * clock.US_PER_HOUR
    history: dict[int, list[tuple[str, int]]] = {}
    ordered_at: dict[int, object] = {}
    payments: set[int] = set()
    for tick in harness.run_ticks(config):
        for op in tick.ops:
            if op.table is Table.PAYMENTS:
                payments.add(as_int(row_of(op)["order_id"]))
            if op.table is not Table.ORDERS:
                continue
            row = row_of(op)
            order_id = as_int(row["order_id"])
            if op.kind is OpKind.INSERT:
                history[order_id] = [("placed", tick.ts_us)]
                ordered_at[order_id] = row["ordered_at"]
                continue
            assert op.kind is OpKind.UPDATE, "an order is never deleted"
            assert row["ordered_at"] == ordered_at[order_id], "ordered_at never changes"
            status = as_str(row["status"])
            if status != history[order_id][-1][0]:
                history[order_id].append((status, tick.ts_us))
    finished = {"delivered": 0, "cancelled": 0}
    for order_id, steps in history.items():
        names = tuple(name for name, _ in steps)
        times = [ts for _, ts in steps]
        assert names in {PAID_PATH[: len(names)], CANCELLED_PATH[: len(names)]}, names
        assert times == sorted(set(times)), "status ticks rise strictly"
        assert times[-1] - times[0] <= budget, "every step falls within L - D of the insert"
        if names[-1] in finished:
            finished[names[-1]] += 1
        if "cancelled" in names:
            assert order_id not in payments, "a cancelled order has no payment"
    assert finished["delivered"] > 50
    assert finished["cancelled"] > 5


# ---------------------------------------------------------------- the capture


def test_the_capture_shares_the_paid_tick_and_equals_the_remaining_gross_less_the_discount() -> (
    None
):
    lines: dict[int, dict[int, Mapping[str, object]]] = {}
    headers: dict[int, Mapping[str, object]] = {}
    captures: dict[int, int] = {}
    payment_ids: list[int] = []
    methods: set[str] = set()
    for tick in harness.run_ticks(order_config()):
        for op in tick.ops:
            row = op.row
            if op.table is Table.ORDER_ITEMS:
                key = (as_int(op.key["order_id"]), as_int(op.key["line_number"]))
                if op.kind is OpKind.DELETE:
                    del lines[key[0]][key[1]]
                else:
                    assert row is not None
                    lines.setdefault(key[0], {})[key[1]] = row
            elif op.table is Table.ORDERS:
                assert row is not None
                headers[as_int(row["order_id"])] = row
            elif op.table is Table.PAYMENTS:
                assert row is not None
                assert op.kind is OpKind.INSERT
                assert row["payment_kind"] == "capture"
                order_id = as_int(row["order_id"])
                captures[order_id] = captures.get(order_id, 0) + 1
                payment_ids.append(as_int(row["payment_id"]))
                header = headers[order_id]
                assert header["status"] == "paid"
                assert kinds(tick) == [
                    (Table.ORDERS, OpKind.UPDATE),
                    (Table.PAYMENTS, OpKind.INSERT),
                ]
                assert row["paid_at"] == clock.to_datetime(tick.ts_us)
                assert row["currency_code"] == header["currency_code"]
                expected = gross_of(lines[order_id]) - cents(header["order_discount"])
                assert cents(row["amount"]) == expected >= 1
                method = as_str(row["payment_method"])
                assert method in PAYMENT_METHODS
                methods.add(method)
    assert sum(captures.values()) > 100
    assert max(captures.values()) == 1, "a paid order has exactly one capture"
    assert payment_ids == list(range(1, len(payment_ids) + 1))
    assert len(methods) > 1


# ---------------------------------------------------------------- line deletes


def inferred_percent(config: ModelConfig, gross: int, discount: int) -> int:
    """The one configured percent that gives this discount on this gross (prices are big enough)."""
    percents = [pct for pct, _ in config.orders.discount_percent_weights]
    matching = [pct for pct in percents if money.half_up_div(gross * pct, 100) == discount]
    assert len(matching) == 1
    return matching[0]


def test_a_line_delete_is_a_pair_and_a_discount_update_in_one_tick_before_payment() -> None:
    config = order_config()
    lines: dict[int, dict[int, Mapping[str, object]]] = {}
    headers: dict[int, Mapping[str, object]] = {}
    percent: dict[int, int] = {}
    deleted: set[tuple[int, int]] = set()
    paid: set[int] = set()
    deletes = 0
    for tick in harness.run_ticks(config):
        first = tick.ops[0]
        if is_placement(tick):
            header = row_of(first)
            order_id = as_int(header["order_id"])
            lines[order_id] = {as_int(row_of(op)["line_number"]): row_of(op) for op in tick.ops[1:]}
            headers[order_id] = header
            percent[order_id] = inferred_percent(
                config, gross_of(lines[order_id]), cents(header["order_discount"])
            )
        elif first.table is Table.ORDER_ITEMS:
            deletes += 1
            assert first.kind is OpKind.UPDATE
            assert kinds(tick) == [
                (Table.ORDER_ITEMS, OpKind.UPDATE),
                (Table.ORDER_ITEMS, OpKind.DELETE),
                (Table.ORDERS, OpKind.UPDATE),
            ]
            order_id = as_int(first.key["order_id"])
            number = as_int(first.key["line_number"])
            assert order_id not in paid, "a line delete falls before the pay tick"
            assert tick.ops[1].key == first.key
            stamp = clock.to_datetime(tick.ts_us)
            assert row_of(first) == {**lines[order_id][number], "updated_at": stamp}
            del lines[order_id][number]
            assert lines[order_id], "an order never loses its last line"
            assert (order_id, number) not in deleted, "a line is deleted at most once"
            deleted.add((order_id, number))
            header = row_of(tick.ops[2])
            assert header["status"] == "placed"
            assert header == {
                **headers[order_id],
                "order_discount": header["order_discount"],
                "updated_at": stamp,
            }
            gross = gross_of(lines[order_id])
            discount = cents(header["order_discount"])
            assert discount == money.half_up_div(gross * percent[order_id], 100)
            assert 0 <= discount <= gross
            headers[order_id] = header
        else:
            for op in tick.ops:
                if op.table is Table.ORDERS:
                    row = row_of(op)
                    headers[as_int(row["order_id"])] = row
                    if row["status"] == "paid":
                        paid.add(as_int(row["order_id"]))
    assert deletes > 20
    assert all(number not in lines[order_id] for order_id, number in deleted)


def test_a_line_number_is_never_reused_after_its_delete() -> None:
    inserted: set[tuple[int, int]] = set()
    for tick in harness.run_ticks(order_config()):
        for op in tick.ops:
            if op.table is Table.ORDER_ITEMS and op.kind is OpKind.INSERT:
                key = (as_int(op.key["order_id"]), as_int(op.key["line_number"]))
                assert key not in inserted
                inserted.add(key)
    assert len(inserted) > 100


# ---------------------------------------------------------------- no parties, and D-09


@pytest.mark.parametrize(
    "volumes",
    [
        {"initial_customers": 0, "customers_per_day": 0},
        {"initial_products": 0, "products_per_day": 0},
    ],
    ids=["no-customer", "no-product"],
)
def test_an_arrival_with_no_live_customer_or_product_is_a_noop_that_still_draws(
    volumes: dict[str, int],
) -> None:
    base = order_config()
    config = dataclasses.replace(base, volumes=dataclasses.replace(base.volumes, **volumes))
    engine = Engine.new(config)
    ticks = harness.commit_all(MemoryCdcSink(), engine.run_until(config.end_us))
    tables = {op.table for tick in ticks for op in tick.ops}
    assert not tables & {Table.ORDERS, Table.ORDER_ITEMS, Table.PAYMENTS}
    assert engine.state.next_ids.get("order") == 1, "no order_id is consumed"
    assert engine.state.streams[StreamName.ORDERS].words > 0, "the arrival's draws are consumed"


def placements_of(config: ModelConfig) -> list[tuple[int, list[bytes]]]:
    """Each placement tick as its instant and its canonical lines, numbered from zero."""
    return [
        (tick.ts_us, [canon.line(0, op) for op in tick.ops])
        for tick in harness.run_ticks(config)
        if is_placement(tick)
    ]


def test_cancel_ppm_never_moves_a_placement_tick() -> None:
    never = order_config(cancel_ppm=0, seed=9)
    always = order_config(cancel_ppm=1_000_000, seed=9)
    kept = placements_of(never)
    assert len(kept) > 100
    assert kept == placements_of(always)
    statuses = {
        row_of(op)["status"]
        for tick in harness.run_ticks(always)
        for op in tick.ops
        if op.table is Table.ORDERS
    }
    assert statuses == {"placed", "cancelled"}, "the always-cancel run really cancelled them all"
