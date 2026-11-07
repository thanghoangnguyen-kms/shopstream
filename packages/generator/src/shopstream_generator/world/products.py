"""Product draws, records and REF section 7 rows (BIZ-02).

The draw table for a new product, in this order and the same count whatever happens:

- products stream: category `pick` over the category list, then the list price in cents with
  `between(list_price_min_cents, list_price_max_cents)` (a launch's gap is drawn by the engine
  before these);
- text stream: adjective `pick`, then noun `pick`.

A record holds indices and integer cents; `row` builds the name from the word lists and the
price through `money.cents_to_decimal`, the only place a price becomes a Decimal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .. import clock, money, textgen
from ..config import Prices
from ..countries import ReferenceDataError
from ..rng import Stream, StreamName
from ..state import ProductRec


@dataclass(frozen=True)
class ProductDraws:
    category_idx: int
    price_cents: int
    adjective_idx: int
    noun_idx: int


def _index(value: int | None) -> int:
    """A `pick` over a non-empty list is never None; an empty list is a reference-data bug."""
    if value is None:
        raise ReferenceDataError("a word list is empty")
    return value


def draw_new_product(streams: Mapping[StreamName, Stream], prices: Prices) -> ProductDraws:
    """The four draws a new product takes, in the table's order."""
    products = streams[StreamName.PRODUCTS]
    text = streams[StreamName.TEXT]
    category_idx = _index(products.pick(len(textgen.words("categories"))))
    price_cents = products.between(prices.list_price_min_cents, prices.list_price_max_cents)
    adjective_idx = _index(text.pick(len(textgen.words("product_adjectives"))))
    noun_idx = _index(text.pick(len(textgen.words("product_nouns"))))
    return ProductDraws(category_idx, price_cents, adjective_idx, noun_idx)


def new_record(product_id: int, ts_us: int, draws: ProductDraws) -> ProductRec:
    """A fresh product: not deleted, priced in integer cents."""
    return ProductRec(
        product_id=product_id,
        created_us=ts_us,
        deleted_us=None,
        adjective_idx=draws.adjective_idx,
        noun_idx=draws.noun_idx,
        category_idx=draws.category_idx,
        list_price_cents=draws.price_cents,
    )


def row(rec: ProductRec, updated_us: int) -> dict[str, object]:
    """The seven `products` columns; `updated_us` is the tick that writes the row."""
    return {
        "product_id": rec.product_id,
        "name": textgen.product_name(rec.adjective_idx, rec.noun_idx),
        "category": textgen.category(rec.category_idx),
        "list_price": money.cents_to_decimal(rec.list_price_cents),
        "deleted_at": None if rec.deleted_us is None else clock.to_datetime(rec.deleted_us),
        "created_at": clock.to_datetime(rec.created_us),
        "updated_at": clock.to_datetime(updated_us),
    }
