"""The discrete-event engine: a heap of due items and the tick rule (D-01 to D-04, D-14).

The CDC heap holds `(due_us, seq, kind, a, b)` tuples of ints. `run_until` looks at the first
item, works out the tick it would get (`clock.next_tick`), and stops before popping it if that
tick is at or after `until`: the item stays in the heap and nothing is flushed (D-02). So the
state at any `until` is a function of seed, config and `until` alone, never of chunking.

A handler returns a `Tick`, or None when its item resolves to nothing. Only a `Tick` advances
the clock and the tick sequence, and every tick carries at least one op (D-01).

An arrival reschedules itself from its due time, not from the tick it was shifted to, so
arrival times stay a renewal process that ties don't disturb (D-03).

Output paths never iterate a set or rely on dict order.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from enum import IntEnum

from . import clock
from .config import ModelConfig
from .ops import Op, OpKind, Table, Tick
from .rng import StreamName
from .state import CUSTOMER, ORDER, PAYMENT, PRODUCT, EngineState, HeapItem, OrderRec, ProductRec
from .world import customers, orders, payments, products, updates


class ItemKind(IntEnum):
    """Heap item kinds. The values persist in a checkpoint later: append, never renumber."""

    INITIAL_PRODUCT = 1
    INITIAL_CUSTOMER = 2
    CUSTOMER_ARRIVAL = 3
    PRODUCT_ARRIVAL = 4
    UPDATE_ARRIVAL = 5
    ORDER_ARRIVAL = 6
    ORDER_STEP = 7  # `a` is the order id, `b` the OrderStep
    LINE_DELETE = 8  # `a` is the order id, `b` the index of the line


class Engine:
    def __init__(self, config: ModelConfig, state: EngineState) -> None:
        self.config = config
        self.state = state
        # Derived, never serialized: ascending ids of the live customers and products, rebuilt
        # from the records here and kept up to date on insert, soft delete and discontinuation.
        # The kill/resume property proves the rebuild.
        self.live_customers: list[int] = sorted(
            record.customer_id
            for record in state.world.customers.values()
            if record.deleted_us is None
        )
        self.live_products: list[int] = sorted(
            record.product_id
            for record in state.world.products.values()
            if record.deleted_us is None
        )

    @classmethod
    def new(cls, config: ModelConfig) -> Engine:
        """A fresh engine: the initial catalogue, the initial customers, then the first arrivals.

        The world starts empty at t0 and the catalogue comes first (D-14): the initial products
        are due 1 microsecond apart from the range start, the initial customers follow them, and
        the first sign-up, product launch and update are one drawn gap after the start. A zero
        rate draws no gap and schedules nothing.
        """
        engine = cls(config, EngineState.new(config))
        volumes = config.volumes
        for index in range(volumes.initial_products):
            engine.push(config.start_us + index, ItemKind.INITIAL_PRODUCT)
        first_customer_us = config.start_us + volumes.initial_products
        for index in range(volumes.initial_customers):
            engine.push(first_customer_us + index, ItemKind.INITIAL_CUSTOMER)
        if volumes.customers_per_day > 0:
            engine.push(config.start_us + engine._customer_gap(), ItemKind.CUSTOMER_ARRIVAL)
        if volumes.products_per_day > 0:
            engine.push(config.start_us + engine._product_gap(), ItemKind.PRODUCT_ARRIVAL)
        if volumes.updates_per_day > 0:
            engine.push(config.start_us + engine._update_gap(), ItemKind.UPDATE_ARRIVAL)
        if volumes.orders_per_day > 0:
            engine.push(config.start_us + engine._order_gap(), ItemKind.ORDER_ARRIVAL)
        return engine

    @classmethod
    def resume(cls, config: ModelConfig, data: object) -> Engine:
        """An engine over a serialized state; raises StateError for another model's state (D-24)."""
        return cls(config, EngineState.from_json(data, config))

    def push(self, due_us: int, kind: ItemKind, a: int = 0, b: int = 0) -> None:
        """Add a CDC item; its monotonic `seq` breaks ties in push order."""
        state = self.state
        state.heap_seq += 1
        heapq.heappush(state.cdc, (due_us, state.heap_seq, int(kind), a, b))

    def _customer_gap(self) -> int:
        """A uniform gap in [1, 2 * mean] microseconds: integer-only, mean preserved (D-03)."""
        mean_gap_us = clock.US_PER_DAY // self.config.volumes.customers_per_day
        return self.state.streams[StreamName.CUSTOMERS].between(1, 2 * mean_gap_us)

    def _product_gap(self) -> int:
        """A uniform gap in [1, 2 * mean] microseconds from the products stream (D-03)."""
        mean_gap_us = clock.US_PER_DAY // self.config.volumes.products_per_day
        return self.state.streams[StreamName.PRODUCTS].between(1, 2 * mean_gap_us)

    def _update_gap(self) -> int:
        """A uniform gap in [1, 2 * mean] microseconds from the lifecycle stream (D-03)."""
        mean_gap_us = clock.US_PER_DAY // self.config.volumes.updates_per_day
        return self.state.streams[StreamName.LIFECYCLE].between(1, 2 * mean_gap_us)

    def _order_gap(self) -> int:
        """A uniform gap in [1, 2 * mean] microseconds from the orders stream (D-03)."""
        mean_gap_us = clock.US_PER_DAY // self.config.volumes.orders_per_day
        return self.state.streams[StreamName.ORDERS].between(1, 2 * mean_gap_us)

    def run_until(self, until_us: int) -> Iterator[Tick]:
        """Yield every tick whose timestamp is before `until_us`, in order."""
        state = self.state
        while state.cdc:
            due_us = state.cdc[0][0]
            ts_us = clock.next_tick(due_us, state.last_ts_us)
            if ts_us >= until_us:
                return
            item = heapq.heappop(state.cdc)
            tick = self._dispatch(item, ts_us)
            if tick is not None:
                state.last_ts_us = ts_us
                state.tick_seq = tick.seq
                yield tick

    def _dispatch(self, item: HeapItem, ts_us: int) -> Tick | None:
        due_us, _, kind, a, b = item
        if kind == ItemKind.INITIAL_PRODUCT:
            return self._insert_product(ts_us)
        if kind == ItemKind.INITIAL_CUSTOMER:
            return self._insert_customer(ts_us)
        if kind == ItemKind.PRODUCT_ARRIVAL:
            self.push(due_us + self._product_gap(), ItemKind.PRODUCT_ARRIVAL)
            return self._insert_product(ts_us)
        if kind == ItemKind.CUSTOMER_ARRIVAL:
            self.push(due_us + self._customer_gap(), ItemKind.CUSTOMER_ARRIVAL)
            return self._insert_customer(ts_us)
        if kind == ItemKind.UPDATE_ARRIVAL:
            self.push(due_us + self._update_gap(), ItemKind.UPDATE_ARRIVAL)
            return self._apply_update(ts_us)
        if kind == ItemKind.ORDER_ARRIVAL:
            self.push(due_us + self._order_gap(), ItemKind.ORDER_ARRIVAL)
            return self._place_order(ts_us)
        if kind == ItemKind.ORDER_STEP:
            return self._order_step(a, orders.OrderStep(b), ts_us)
        if kind == ItemKind.LINE_DELETE:
            return self._delete_line(a, b, ts_us)
        raise ValueError(f"no handler for item kind {kind}")

    def _insert_customer(self, ts_us: int) -> Tick:
        state = self.state
        draws = customers.draw_new_customer(state.streams)
        customer_id = state.next_ids[CUSTOMER]
        state.next_ids[CUSTOMER] = customer_id + 1
        record = customers.new_record(customer_id, ts_us, draws)
        state.world.customers[customer_id] = record
        self.live_customers.append(customer_id)
        op = Op(
            Table.CUSTOMERS,
            OpKind.INSERT,
            {"customer_id": customer_id},
            customers.row(record, ts_us),
        )
        return Tick(state.tick_seq + 1, ts_us, (op,))

    def _insert_product(self, ts_us: int) -> Tick:
        state = self.state
        draws = products.draw_new_product(state.streams, self.config.prices)
        product_id = state.next_ids[PRODUCT]
        state.next_ids[PRODUCT] = product_id + 1
        record = products.new_record(product_id, ts_us, draws)
        state.world.products[product_id] = record
        self.live_products.append(product_id)
        op = Op(
            Table.PRODUCTS,
            OpKind.INSERT,
            {"product_id": product_id},
            products.row(record, ts_us),
        )
        return Tick(state.tick_seq + 1, ts_us, (op,))

    def _apply_update(self, ts_us: int) -> Tick | None:
        """Draw every update number, then apply it; a missing target is a no-op, not a tick."""
        state = self.state
        draws = updates.draw_update(
            state.streams, self.config, len(self.live_customers), len(self.live_products)
        )
        op = updates.apply_update(state, self.live_customers, self.live_products, ts_us, draws)
        if op is None:
            return None
        return Tick(state.tick_seq + 1, ts_us, (op,))

    # ------------------------------------------------------------ orders

    def _place_order(self, ts_us: int) -> Tick | None:
        """Draw every order number, then place the order; no live customer or product is a no-op."""
        state = self.state
        draws = orders.draw_order(
            state.streams, self.config, len(self.live_customers), len(self.live_products)
        )
        if draws.customer_pick is None:
            return None
        lines: list[ProductRec] = []
        for pick in draws.product_picks[: draws.line_count]:
            if pick is None:
                return None
            lines.append(state.world.products[self.live_products[pick]])
        customer = state.world.customers[self.live_customers[draws.customer_pick]]
        order_id = state.next_ids[ORDER]
        state.next_ids[ORDER] = order_id + 1
        rec, ops = orders.place_order(order_id, ts_us, customer, lines, draws)
        state.world.orders[order_id] = rec
        self._schedule_lifecycle(rec, ts_us, draws)
        return Tick(state.tick_seq + 1, ts_us, ops)

    def _hold(self, rec: OrderRec, due_us: int, kind: ItemKind, b: int = 0) -> None:
        """Push an item that names the order and count it, so the record outlives it (C9)."""
        rec.pending += 1
        self.push(due_us, kind, rec.order_id, b)

    def _release(self, rec: OrderRec) -> None:
        """An item named the order and has fired; the record goes with its last item."""
        rec.pending -= 1
        if rec.pending == 0:
            del self.state.world.orders[rec.order_id]

    def _schedule_lifecycle(self, rec: OrderRec, ts_us: int, draws: orders.OrderDraws) -> None:
        """Schedule every later item as an absolute due time from the insert tick (D-15)."""
        paid_due = ts_us + draws.pay_after
        if draws.cancel:
            self._hold(rec, paid_due, ItemKind.ORDER_STEP, orders.OrderStep.CANCELLED)
            return
        plan = draws.line_delete_plan()
        if plan is not None:
            line_index, offset = plan
            self._hold(rec, ts_us + offset, ItemKind.LINE_DELETE, line_index)
        shipped_due = paid_due + draws.ship_after
        delivered_due = shipped_due + draws.deliver_after
        self._hold(rec, paid_due, ItemKind.ORDER_STEP, orders.OrderStep.PAID)
        self._hold(rec, shipped_due, ItemKind.ORDER_STEP, orders.OrderStep.SHIPPED)
        self._hold(rec, delivered_due, ItemKind.ORDER_STEP, orders.OrderStep.DELIVERED)

    def _order_step(self, order_id: int, step: orders.OrderStep, ts_us: int) -> Tick:
        """Move the order to its next status; payment also inserts the capture in the same tick."""
        state = self.state
        rec = state.world.orders[order_id]
        rec.status = int(step)
        ops = [
            Op(Table.ORDERS, OpKind.UPDATE, {"order_id": order_id}, orders.order_row(rec, ts_us))
        ]
        if step is orders.OrderStep.PAID:
            amount = orders.gross_cents(rec) - rec.discount_cents
            rec.capture_cents = amount
            payment_id = state.next_ids[PAYMENT]
            state.next_ids[PAYMENT] = payment_id + 1
            row = payments.payment_row(payment_id, rec, payments.CAPTURE, amount, ts_us)
            ops.append(Op(Table.PAYMENTS, OpKind.INSERT, {"payment_id": payment_id}, row))
        self._release(rec)
        return Tick(state.tick_seq + 1, ts_us, tuple(ops))

    def _delete_line(self, order_id: int, line_index: int, ts_us: int) -> Tick:
        """Delete one line as an update then a delete, and recompute the discount, in one tick."""
        state = self.state
        rec = state.world.orders[order_id]
        key = {"order_id": order_id, "line_number": line_index + 1}
        last_row = orders.line_row(rec, line_index, ts_us)
        deleted = list(rec.line_deleted)
        deleted[line_index] = 1
        rec.line_deleted = tuple(deleted)
        rec.discount_cents = orders.discount_cents(orders.gross_cents(rec), rec.discount_pct)
        ops = (
            Op(Table.ORDER_ITEMS, OpKind.UPDATE, key, last_row),
            Op(Table.ORDER_ITEMS, OpKind.DELETE, key, None),
            Op(Table.ORDERS, OpKind.UPDATE, {"order_id": order_id}, orders.order_row(rec, ts_us)),
        )
        self._release(rec)
        return Tick(state.tick_seq + 1, ts_us, ops)
