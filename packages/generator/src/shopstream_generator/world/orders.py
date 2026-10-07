"""Orders: the placement draws, the record built at placement and REF section 7 rows (BIZ-03, BIZ-04, BIZ-07).

One order arrival draws the same numbers whether or not it places an order and whichever branch
fires (D-09), in this order. The arrival's own gap is drawn by the engine first, when it
reschedules, from the orders stream:

- orders stream: customer `pick` over the live customers; line count `between(1, max_lines)`;
  for each of `max_lines` slots a product `pick` over the live products and a quantity
  `between(1, max_quantity)`; the discount percent `choice_index`; cancel `bernoulli_ppm`;
  pay, ship and deliver offsets `between`; line-delete `bernoulli_ppm`; the deleted line `pick`
  over the line count; the delete offset `pick` over `pay_after - 1`;
- payments stream: method `choice_index`; refund `bernoulli_ppm`; refund offset `between`; refund
  percent `choice_index`;
- reviews stream: review `bernoulli_ppm`; review offset `between`; the reviewed line `pick` over
  the surviving lines; rating `choice_index`; moderation `bernoulli_ppm`; moderation offset
  `between`;
- text stream: the opener, detail and closer `pick` over the three review lists.

Everything is drawn at placement and kept in the `OrderRec`, so no later step draws and a
cancelled order shifts nothing. Money is integer cents; a `Decimal` is built only in a row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from .. import clock, countries, fx, money, textgen
from ..config import ModelConfig
from ..countries import ReferenceDataError
from ..ops import Op, OpKind, Table
from ..rng import Stream, StreamName
from ..state import CustomerRec, OrderRec, ProductRec

# Sorted ISO codes of the 30 latest quotes: an order stores an index, never the code.
CURRENCIES: tuple[str, ...] = tuple(sorted(fx.latest_quotes()))
EURO = "EUR"
_DISCOUNT_BASE = 100


class OrderStatus(IntEnum):
    """An order's status; the names are the lower-case column values."""

    PLACED = 0
    PAID = 1
    SHIPPED = 2
    DELIVERED = 3
    CANCELLED = 4


class OrderStep(IntEnum):
    """A status step a heap item carries: each equals the status it moves the order to."""

    PAID = OrderStatus.PAID
    SHIPPED = OrderStatus.SHIPPED
    DELIVERED = OrderStatus.DELIVERED
    CANCELLED = OrderStatus.CANCELLED


@dataclass(frozen=True)
class OrderDraws:
    """Every number one order arrival draws; a pick is None only over an empty population."""

    customer_pick: int | None
    line_count: int
    product_picks: tuple[int | None, ...]
    quantities: tuple[int, ...]
    discount_pct: int
    cancel: bool
    pay_after: int
    ship_after: int
    deliver_after: int
    line_delete: bool
    delete_line: int | None
    delete_offset: int | None
    method_idx: int
    refund: bool
    refund_after: int
    refund_percent: int
    review: bool
    review_after: int
    review_line: int | None
    rating: int
    moderation: bool
    moderation_after: int
    opener_idx: int
    detail_idx: int
    closer_idx: int

    def line_delete_plan(self) -> tuple[int, int] | None:
        """`(line index, due offset)` when a line delete applies, else None."""
        return _line_delete_plan(
            self.cancel, self.line_delete, self.line_count, self.delete_line, self.delete_offset
        )


def _line_delete_plan(
    cancel: bool,
    flag: bool,
    line_count: int,
    delete_line: int | None,
    delete_offset: int | None,
) -> tuple[int, int] | None:
    """A line delete applies when the order isn't cancelled, the flag fired, the order starts
    with two lines and both picks exist. The offset is at least 1 and below `pay_after`, so the
    delete is due strictly before the pay step.
    """
    if cancel or not flag or line_count < 2 or delete_line is None or delete_offset is None:
        return None
    return delete_line, delete_offset + 1


def _index(value: int | None) -> int:
    """A `pick` over a non-empty list is never None; an empty list is a reference-data bug."""
    if value is None:
        raise ReferenceDataError("a word list is empty")
    return value


def draw_order(
    streams: Mapping[StreamName, Stream],
    config: ModelConfig,
    live_customer_count: int,
    live_product_count: int,
) -> OrderDraws:
    """The draws of one order arrival, in the table's order, whatever branch fires."""
    orders = streams[StreamName.ORDERS]
    payments = streams[StreamName.PAYMENTS]
    reviews = streams[StreamName.REVIEWS]
    text = streams[StreamName.TEXT]
    shape = config.orders
    ppm = config.business_ppm
    life = config.lifecycle_us

    customer_pick = orders.pick(live_customer_count)
    line_count = orders.between(1, shape.max_lines)
    product_picks: list[int | None] = []
    quantities: list[int] = []
    for _ in range(shape.max_lines):
        product_picks.append(orders.pick(live_product_count))
        quantities.append(orders.between(1, shape.max_quantity))
    discount_pct = shape.discount_percent_weights[
        orders.choice_index([weight for _, weight in shape.discount_percent_weights])
    ][0]
    cancel = orders.bernoulli_ppm(ppm.cancel)
    pay_after = orders.between(*life.pay_after)
    ship_after = orders.between(*life.ship_after_pay)
    deliver_after = orders.between(*life.deliver_after_ship)
    line_delete = orders.bernoulli_ppm(ppm.line_delete)
    delete_line = orders.pick(line_count)
    delete_offset = orders.pick(pay_after - 1)

    method_idx = payments.choice_index(config.payments.method_weights)
    refund = payments.bernoulli_ppm(ppm.refund)
    refund_after = payments.between(*life.refund_after_delivery)
    percents = config.payments.refund_percent_weights
    refund_percent = percents[payments.choice_index([weight for _, weight in percents])][0]

    plan = _line_delete_plan(cancel, line_delete, line_count, delete_line, delete_offset)
    surviving = line_count - (1 if plan is not None else 0)
    review = reviews.bernoulli_ppm(ppm.review)
    review_after = reviews.between(*life.review_after_delivery)
    review_line = reviews.pick(surviving)
    ratings = config.reviews.rating_weights
    rating = ratings[reviews.choice_index([weight for _, weight in ratings])][0]
    moderation = reviews.bernoulli_ppm(ppm.moderation)
    moderation_after = reviews.between(*life.moderation_after_review)

    opener_idx = _index(text.pick(len(textgen.words("review_openers"))))
    detail_idx = _index(text.pick(len(textgen.words("review_details"))))
    closer_idx = _index(text.pick(len(textgen.words("review_closers"))))
    return OrderDraws(
        customer_pick,
        line_count,
        tuple(product_picks),
        tuple(quantities),
        discount_pct,
        cancel,
        pay_after,
        ship_after,
        deliver_after,
        line_delete,
        delete_line,
        delete_offset,
        method_idx,
        refund,
        refund_after,
        refund_percent,
        review,
        review_after,
        review_line,
        rating,
        moderation,
        moderation_after,
        opener_idx,
        detail_idx,
        closer_idx,
    )


def gross_cents(rec: OrderRec) -> int:
    """The positive gross: quantity times unit price over the lines that are still there."""
    return sum(
        quantity * unit
        for quantity, unit, deleted in zip(
            rec.line_quantities, rec.line_unit_cents, rec.line_deleted, strict=True
        )
        if not deleted
    )


def discount_cents(gross: int, percent: int) -> int:
    """`gross * percent / 100` rounded half up, in integer cents (D-13)."""
    return money.half_up_div(gross * percent, _DISCOUNT_BASE)


def place_order(
    order_id: int,
    ts_us: int,
    customer: CustomerRec,
    products: Sequence[ProductRec],
    draws: OrderDraws,
) -> tuple[OrderRec, tuple[Op, ...]]:
    """Build the order record and its placement ops from `draws`.

    `products` are the records of the drawn lines, in line order. The currency follows the
    customer's current country. A non-EUR unit price is the list price converted by that
    currency's fixed latest quote (D-11), never a rate keyed by date.
    """
    code = countries.countries()[customer.country_idx].currency
    quote = fx.latest_quotes()[code]
    unit_cents = tuple(
        product.list_price_cents
        if code == EURO
        else money.convert_cents(product.list_price_cents, quote)
        for product in products
    )
    quantities = draws.quantities[: len(products)]
    rec = OrderRec(
        order_id=order_id,
        customer_id=customer.customer_id,
        currency_idx=CURRENCIES.index(code),
        ordered_us=ts_us,
        discount_pct=draws.discount_pct,
        status=int(OrderStatus.PLACED),
        discount_cents=0,
        capture_cents=None,
        method_idx=draws.method_idx,
        line_products=tuple(product.product_id for product in products),
        line_quantities=quantities,
        line_unit_cents=unit_cents,
        line_deleted=tuple(0 for _ in products),
        refund_flag=int(draws.refund),
        refund_percent=draws.refund_percent,
        review_flag=int(draws.review),
        review_line=_index(draws.review_line),
        rating=draws.rating,
        opener_idx=draws.opener_idx,
        detail_idx=draws.detail_idx,
        closer_idx=draws.closer_idx,
        moderation_flag=int(draws.moderation),
        review_id=None,
        review_created_us=None,
        pending=0,
    )
    rec.discount_cents = discount_cents(gross_cents(rec), rec.discount_pct)
    ops = [Op(Table.ORDERS, OpKind.INSERT, {"order_id": order_id}, order_row(rec, ts_us))]
    for index in range(len(products)):
        key = {"order_id": order_id, "line_number": index + 1}
        ops.append(Op(Table.ORDER_ITEMS, OpKind.INSERT, key, line_row(rec, index, ts_us)))
    return rec, tuple(ops)


def order_row(rec: OrderRec, updated_us: int) -> dict[str, object]:
    """The `orders` columns; `updated_us` is the tick that writes the row."""
    placed = clock.to_datetime(rec.ordered_us)
    return {
        "order_id": rec.order_id,
        "customer_id": rec.customer_id,
        "status": OrderStatus(rec.status).name.lower(),
        "currency_code": CURRENCIES[rec.currency_idx],
        "order_discount": money.cents_to_decimal(rec.discount_cents),
        "ordered_at": placed,
        "created_at": placed,
        "updated_at": clock.to_datetime(updated_us),
    }


def line_row(rec: OrderRec, line_index: int, updated_us: int) -> dict[str, object]:
    """The `order_items` columns of one line; its `line_number` is its index plus one."""
    return {
        "order_id": rec.order_id,
        "line_number": line_index + 1,
        "product_id": rec.line_products[line_index],
        "quantity": rec.line_quantities[line_index],
        "unit_price": money.cents_to_decimal(rec.line_unit_cents[line_index]),
        "created_at": clock.to_datetime(rec.ordered_us),
        "updated_at": clock.to_datetime(updated_us),
    }
