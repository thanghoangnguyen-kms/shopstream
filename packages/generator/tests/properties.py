"""ADR-005's invariants P1 to P6 and P8 as pure checks over a finished run.

Each `check_pN` returns a list of violation strings, empty when the property holds. A message names
the property, the table, the generated ids (counters, never personal data) and counts, and never a
row value: generated rows look personal, and a Hypothesis failure prints the message into public CI
logs that erasure can't reach (the `scripts/row_hash.py` rule, T-02-19).

The checks read a "world": its `ticks`, the `sink` that applied them and each stream's canonical
`lines`. `harness.World` is one; a hand-built stand-in with the same three attributes is how the
tests prove each check can fail. P7 is `test_fx_snapshot.py`'s and isn't here.

The checks replay the ticks themselves instead of trusting the sink: the sink judges one tick at a
time, so a rule that spans ticks (a product that must exist, a lifecycle that must finish) only
holds if something looks across them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from shopstream_generator import clock, fx
from shopstream_generator.config import ModelConfig
from shopstream_generator.ops import PAGE_VIEW, Op, OpKind, Table, Tick
from shopstream_generator.schema import SCHEMAS

Key = tuple[int, ...]
Row = Mapping[str, object]

TERMINAL = ("delivered", "cancelled")
# Ticks shift later by a microsecond per collision, so "far enough before the range end" for a
# lifecycle to have completed leaves a second of room for that shift.
RANGE_END_SLACK_US = clock.US_PER_SECOND
_MICROSECOND = timedelta(microseconds=1)


class TableSource(Protocol):
    """What P1 and P8 read: a table's primary-key tuples and rows, as `MemoryCdcSink.table` gives."""

    def table(self, table: Table) -> Mapping[Key, Row]: ...


class WorldLike(Protocol):
    """A run: `harness.World` or any stand-in with these three attributes."""

    @property
    def ticks(self) -> Sequence[Tick]: ...

    @property
    def sink(self) -> TableSource: ...

    @property
    def lines(self) -> Mapping[str, Sequence[bytes]]: ...


# ---------------------------------------------------------------------------------------------
# Helpers


def micros(moment: datetime) -> int:
    """Microseconds since the epoch of an aware datetime."""
    return (moment - clock.EPOCH) // _MICROSECOND


def key_of(op: Op) -> Key:
    """An op's key as a tuple in the order its own mapping lists the columns."""
    return tuple(op.key.values())


def _id(op: Op) -> int:
    """The leading id of an op's key: the order id for a line, the row id otherwise."""
    return next(iter(op.key.values()))


def _row(op: Op) -> Row:
    """The after-image of a non-delete op."""
    if op.row is None:
        raise ValueError("a delete has no row")
    return op.row


def _int(row: Row, column: str) -> int:
    value = row[column]
    if type(value) is not int:
        raise TypeError(f"{column} is not an integer")
    return value


def _time(row: Row, column: str) -> datetime:
    value = row[column]
    if not isinstance(value, datetime):
        raise TypeError(f"{column} is not a timestamp")
    return value


def _optional_time(row: Row, column: str) -> datetime | None:
    value = row[column]
    if value is None:
        return None
    return _time(row, column)


def _money(row: Row, column: str) -> Decimal:
    value = row[column]
    if not isinstance(value, Decimal):
        raise TypeError(f"{column} is not a decimal")
    return value


def _text(row: Row, column: str) -> str:
    value = row[column]
    if not isinstance(value, str):
        raise TypeError(f"{column} is not text")
    return value


def _live_at(deleted_at: datetime | None, moment: datetime) -> bool:
    """True when a row soft-deleted at `deleted_at` (or never) is still live at `moment`."""
    return deleted_at is None or moment < deleted_at


# ---------------------------------------------------------------------------------------------
# P1: the final state's primary keys


def check_p1(world: WorldLike) -> list[str]:
    """P1: each table's keys are unique, non-null and equal to the REF section 3 grain key.

    The grain key is the schema's primary key: one column everywhere but `order_items`, whose key
    is the pair `(order_id, line_number)`. Whole tuples are compared, so two lines that share an
    order id or a line number stay distinct, and the order the rows are listed in changes nothing.
    The `page_view` stream is empty until Phase 3.
    """
    problems: list[str] = []
    for table in Table:
        columns = SCHEMAS[table].primary_key
        rows = world.sink.table(table)
        bad_key = 0
        mismatched = 0
        claimed: set[tuple[object, ...]] = set()
        repeated = 0
        first_bad: Key | None = None
        for key, row in rows.items():
            shape_ok = len(key) == len(columns) and all(type(part) is int for part in key)
            from_row = tuple(row.get(column) for column in columns)
            if not shape_ok:
                bad_key += 1
            elif from_row != key:
                mismatched += 1
            if (not shape_ok or from_row != key) and first_bad is None:
                first_bad = key
            if from_row in claimed:
                repeated += 1
            claimed.add(from_row)
        label = f"P1 {table.value}"
        if bad_key:
            problems.append(f"{label}: {bad_key} key(s) are null, not integers or the wrong width")
        if mismatched:
            problems.append(
                f"{label}: {mismatched} key(s) differ from the row's {'+'.join(columns)} "
                f"(first {first_bad})"
            )
        if repeated:
            problems.append(f"{label}: {repeated} row(s) repeat another row's {'+'.join(columns)}")
    page_views = len(world.lines.get(PAGE_VIEW, ()))
    if page_views:
        problems.append(
            f"P1 {PAGE_VIEW}: {page_views} line(s) in a stream that is empty in Phase 2"
        )
    return problems


# ---------------------------------------------------------------------------------------------
# P2: references hold when they are made


class _Existing:
    """The rows P2 needs as of the end of the previous tick: who was there, and when they left."""

    def __init__(self) -> None:
        self.customers: dict[int, tuple[datetime, datetime | None]] = {}
        self.products: dict[int, datetime | None] = {}
        self.orders: set[int] = set()

    def apply(self, op: Op) -> None:
        if op.table is Table.CUSTOMERS and op.kind is not OpKind.DELETE:
            row = _row(op)
            self.customers[_id(op)] = (_time(row, "created_at"), _optional_time(row, "deleted_at"))
        elif op.table is Table.PRODUCTS and op.kind is not OpKind.DELETE:
            self.products[_id(op)] = _optional_time(_row(op), "deleted_at")
        elif op.table is Table.ORDERS and op.kind is OpKind.INSERT:
            self.orders.add(_id(op))


def check_p2(world: WorldLike) -> list[str]:
    """P2: every reference names a row that existed, live, when it was made.

    An order names a customer present at its tick, with `ordered_at` in `[created_at, deleted_at)`;
    a line names a product inserted at an earlier tick and not discontinued by then, and an order
    that exists; a payment names an existing order; a review names an existing product and a
    customer who isn't soft-deleted at the review's tick.

    "Lines committed before `t1 - K` have their product" and "an identified page view falls in its
    customer's lifetime" hold trivially here: no late product and no clickstream yet (FA-02-03).
    """
    problems: list[str] = []
    existing = _Existing()
    for tick in world.ticks:
        now = clock.to_datetime(tick.ts_us)
        seen_orders = set(existing.orders)
        for op in tick.ops:
            if op.kind is not OpKind.INSERT:
                continue
            row = _row(op)
            where = f"P2 {op.table.value} {_id(op)} at tick {tick.seq}"
            if op.table is Table.ORDERS:
                seen_orders.add(_id(op))
                problems.extend(_order_customer(where, row, existing))
            elif op.table is Table.ORDER_ITEMS:
                if _id(op) not in seen_orders:
                    problems.append(f"{where}: its order doesn't exist")
                product = _int(row, "product_id")
                if product not in existing.products:
                    problems.append(
                        f"{where}: product {product} wasn't inserted at an earlier tick"
                    )
                elif not _live_at(existing.products[product], now):
                    problems.append(f"{where}: product {product} was discontinued by then")
            elif op.table is Table.PAYMENTS:
                order = _int(row, "order_id")
                if order not in seen_orders:
                    problems.append(f"{where}: order {order} doesn't exist")
            elif op.table is Table.REVIEWS:
                problems.extend(_review_refs(where, row, existing))
        for op in tick.ops:
            existing.apply(op)
    return problems


def _order_customer(where: str, row: Row, existing: _Existing) -> list[str]:
    customer = _int(row, "customer_id")
    entry = existing.customers.get(customer)
    if entry is None:
        return [f"{where}: customer {customer} doesn't exist"]
    created, deleted = entry
    ordered = _time(row, "ordered_at")
    if not created <= ordered or not _live_at(deleted, ordered):
        return [f"{where}: ordered_at lies outside customer {customer}'s lifetime"]
    return []


def _review_refs(where: str, row: Row, existing: _Existing) -> list[str]:
    problems: list[str] = []
    product = _int(row, "product_id")
    if product not in existing.products:
        problems.append(f"{where}: product {product} doesn't exist")
    customer = _int(row, "customer_id")
    entry = existing.customers.get(customer)
    if entry is None:
        problems.append(f"{where}: customer {customer} doesn't exist")
    elif entry[1] is not None:
        problems.append(f"{where}: customer {customer} is soft-deleted")
    return problems


# ---------------------------------------------------------------------------------------------
# P3: ticks and updated_at


def check_p3(ticks: Sequence[Tick]) -> list[str]:
    """P3: ticks run 1, 2, 3 and rise strictly; every written row's `updated_at` is its tick; a
    key changes at most once per tick, except an update immediately followed by its own delete.
    """
    problems: list[str] = []
    expected_seq = 1
    previous_ts: int | None = None
    for tick in ticks:
        if tick.seq != expected_seq:
            problems.append(f"P3 tick {tick.seq}: expected seq {expected_seq}")
        expected_seq = tick.seq + 1
        if previous_ts is not None and tick.ts_us <= previous_ts:
            problems.append(f"P3 tick {tick.seq}: ts doesn't rise strictly")
        previous_ts = tick.ts_us
        stamp = clock.to_datetime(tick.ts_us)
        seen: set[tuple[Table, Key]] = set()
        before: Op | None = None
        for op in tick.ops:
            if op.row is not None and op.row.get("updated_at") != stamp:
                problems.append(
                    f"P3 {op.table.value} {_id(op)} at tick {tick.seq}: updated_at isn't the tick"
                )
            if (op.table, key_of(op)) in seen and not _is_delete_pair(before, op):
                problems.append(
                    f"P3 {op.table.value} {_id(op)} at tick {tick.seq}: key changes twice"
                )
            seen.add((op.table, key_of(op)))
            before = op
    return problems


def _is_delete_pair(before: Op | None, op: Op) -> bool:
    """True when `op` is a delete that immediately follows an update of the same key."""
    return (
        before is not None
        and op.kind is OpKind.DELETE
        and before.kind is OpKind.UPDATE
        and before.table is op.table
        and key_of(before) == key_of(op)
    )


# ---------------------------------------------------------------------------------------------
# P4: event times, the edit horizon and the lifecycle


def check_p4(world: WorldLike, config: ModelConfig) -> list[str]:
    """P4: `ordered_at` is the insert tick; nothing touches an order more than L after it; every
    order placed early enough finishes within L - D; a terminal status is final.

    Phase 2 has no late-order knob, so `ordered_at` equals the insert's tick, which is inside the
    `[tick - D, tick]` window ADR-004 C4 allows; Phase 3 widens this check to the window.
    """
    late_max = config.horizons.late_order_max
    horizon = config.horizons.edit_horizon
    life = config.lifecycle_us
    full_lifecycle = life.pay_after[1] + life.ship_after_pay[1] + life.deliver_after_ship[1]
    cutoff = config.end_us - full_lifecycle - RANGE_END_SLACK_US

    problems: list[str] = []
    placed: dict[int, tuple[int, int]] = {}  # order id -> (ordered_us, insert tick seq)
    finished: dict[int, int] = {}  # order id -> the tick ts of its terminal status
    cancelled: set[int] = set()
    for tick in world.ticks:
        for op in tick.ops:
            if op.table not in (Table.ORDERS, Table.ORDER_ITEMS):
                continue
            order = _id(op)
            where = f"P4 {op.table.value} {order} at tick {tick.seq}"
            if op.table is Table.ORDERS and op.kind is OpKind.INSERT:
                ordered = micros(_time(_row(op), "ordered_at"))
                placed[order] = (ordered, tick.ts_us)
                if ordered != tick.ts_us:
                    problems.append(f"{where}: ordered_at isn't the insert tick")
                continue
            if order not in placed:
                problems.append(f"{where}: an op on an order that was never placed")
                continue
            if tick.ts_us - placed[order][0] > horizon:
                problems.append(f"{where}: touched more than L after ordered_at")
            if order in cancelled:
                problems.append(f"{where}: follows the order's cancellation")
            if op.table is Table.ORDERS and op.kind is OpKind.UPDATE:
                if order in finished:
                    problems.append(f"{where}: changed after its terminal status")
                elif _text(_row(op), "status") in TERMINAL:
                    finished[order] = tick.ts_us
                    if _text(_row(op), "status") == "cancelled":
                        cancelled.add(order)
    for order, (_, inserted_ts) in placed.items():
        if inserted_ts > cutoff:
            continue
        if order not in finished:
            problems.append(f"P4 orders {order}: placed early enough but never finished")
        elif finished[order] - inserted_ts > horizon - late_max:
            problems.append(f"P4 orders {order}: finished later than L - D after placement")
    return problems


# ---------------------------------------------------------------------------------------------
# P5: deletes, finality, line numbers, insert-only payments


def check_p5(ticks: Sequence[Tick]) -> list[str]:
    """P5: a hard delete follows an update of its key in the same tick and only on `order_items`
    and `reviews`; a soft-deleted customer never changes again; no `(order_id, line_number)` is
    inserted twice; `payments` only ever sees inserts.
    """
    problems: list[str] = []
    gone: set[int] = set()
    lines_seen: set[Key] = set()
    for tick in ticks:
        before: Op | None = None
        for op in tick.ops:
            where = f"P5 {op.table.value} {_id(op)} at tick {tick.seq}"
            if op.kind is OpKind.DELETE:
                if op.table not in (Table.ORDER_ITEMS, Table.REVIEWS):
                    problems.append(f"{where}: a hard delete on a table that has none")
                if not _is_delete_pair(before, op):
                    problems.append(f"{where}: a delete without an update of the same key")
            if op.table is Table.PAYMENTS and op.kind is not OpKind.INSERT:
                problems.append(f"{where}: payments are insert-only")
            if op.table is Table.CUSTOMERS:
                if _id(op) in gone:
                    problems.append(f"{where}: a soft-deleted customer changed again")
                if op.kind is not OpKind.DELETE and _row(op)["deleted_at"] is not None:
                    gone.add(_id(op))
            if op.table is Table.ORDER_ITEMS and op.kind is OpKind.INSERT:
                if key_of(op) in lines_seen:
                    problems.append(f"{where}: a line number was inserted twice")
                lines_seen.add(key_of(op))
            before = op
    return problems


# ---------------------------------------------------------------------------------------------
# P6: discount, refunds, currency


def check_p6(world: WorldLike) -> list[str]:
    """P6, after every tick and for each order the tick touched: `0 <= order_discount <=` the sum
    of the order's current line amounts (so 0 when that sum is 0); refunds never exceed captures;
    a payment's currency is its order's.

    Checking after every tick means a transient breach a later tick repaired still fails
    (FA-02-04).
    """
    problems: list[str] = []
    discount: dict[int, Decimal] = {}
    currency: dict[int, str] = {}
    lines: dict[int, dict[int, Decimal]] = {}
    captured: dict[int, Decimal] = {}
    refunded: dict[int, Decimal] = {}
    for tick in world.ticks:
        touched: set[int] = set()
        for op in tick.ops:
            order = _id(op)
            if op.table is Table.ORDERS and op.kind is not OpKind.DELETE:
                row = _row(op)
                discount[order] = _money(row, "order_discount")
                currency[order] = _text(row, "currency_code")
                touched.add(order)
            elif op.table is Table.ORDER_ITEMS:
                book = lines.setdefault(order, {})
                line_number = op.key["line_number"]
                if op.kind is OpKind.DELETE:
                    book.pop(line_number, None)
                else:
                    row = _row(op)
                    book[line_number] = _int(row, "quantity") * _money(row, "unit_price")
                touched.add(order)
            elif op.table is Table.PAYMENTS and op.kind is OpKind.INSERT:
                row = _row(op)
                order = _int(row, "order_id")
                if order in currency and _text(row, "currency_code") != currency[order]:
                    problems.append(
                        f"P6 payments {_id(op)} at tick {tick.seq}: currency isn't order {order}'s"
                    )
                book = refunded if _text(row, "payment_kind") == "refund" else captured
                book[order] = book.get(order, Decimal(0)) + _money(row, "amount")
                touched.add(order)
        for order in sorted(touched):
            where = f"P6 orders {order} at tick {tick.seq}"
            total = sum(lines.get(order, {}).values(), Decimal(0))
            if order in discount and not Decimal(0) <= discount[order] <= total:
                problems.append(f"{where}: the discount is outside [0, the sum of its lines]")
            if refunded.get(order, Decimal(0)) > captured.get(order, Decimal(0)):
                problems.append(f"{where}: refunds exceed captures")
    return problems


# ---------------------------------------------------------------------------------------------
# P8: currencies


def check_p8(world: WorldLike) -> list[str]:
    """P8: every order and payment currency is one of the vendored latest ECB quotes."""
    quoted = set(fx.latest_quotes())
    problems: list[str] = []
    for table in (Table.ORDERS, Table.PAYMENTS):
        bad = [
            key
            for key, row in world.sink.table(table).items()
            if row["currency_code"] not in quoted
        ]
        if bad:
            problems.append(
                f"P8 {table.value}: {len(bad)} row(s) have a currency outside the latest quotes "
                f"(first {min(bad)})"
            )
    return problems
