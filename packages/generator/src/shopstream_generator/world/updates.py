"""In-life updates: one process for customer moves and edits, soft deletes, product edits and
discontinuations (BIZ-01, BIZ-02, D-03, D-09, D-17).

An update arrival makes the same draws whatever kind fires and whether a target exists, in this
order (the arrival's own gap is drawn by the engine first, when it reschedules):

- lifecycle stream: kind `choice_index` over the update weights; customer `pick` over the live
  customers; product `pick` over the live products; country `choice_index` over the country
  weights; city `pick` over that drawn country's cities; price `between` the configured cents
  range; category `pick` over the category list;
- text stream: first-name, last-name, adjective and noun `pick`, in that order.

The kind decides which of those draws is used and the rest are thrown away, so two configs with
equal weight totals but different kinds place their update ticks at the same instants (D-09).

A pick is an index into the ascending-id live list, never a set or dict position. A soft-deleted
customer or a discontinued product leaves its live list, so it is never picked again and its
`deleted_at` never changes (C6): each is a final update.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .. import countries, textgen
from ..config import ModelConfig, UpdateKind
from ..countries import ReferenceDataError
from ..ops import Op, OpKind, Table
from ..rng import Stream, StreamName
from ..state import EngineState
from . import customers, products

_CUSTOMER_KINDS = frozenset(
    {
        UpdateKind.CUSTOMER_MOVE,
        UpdateKind.CUSTOMER_EMAIL,
        UpdateKind.CUSTOMER_NAME,
        UpdateKind.CUSTOMER_SOFT_DELETE,
    }
)


@dataclass(frozen=True)
class UpdateDraws:
    """Every number one update arrival draws; a pick is None only over an empty population."""

    kind: UpdateKind
    customer_pick: int | None
    product_pick: int | None
    country_idx: int
    city_pick: int
    price_cents: int
    category_pick: int
    first_idx: int
    last_idx: int
    adjective_idx: int
    noun_idx: int


def _index(value: int | None) -> int:
    """A `pick` over a non-empty list is never None; an empty list is a reference-data bug."""
    if value is None:
        raise ReferenceDataError("a word list or city list is empty")
    return value


def draw_update(
    streams: Mapping[StreamName, Stream],
    config: ModelConfig,
    live_customer_count: int,
    live_product_count: int,
) -> UpdateDraws:
    """The eleven draws of one update arrival, in the table's order."""
    table = countries.countries()
    lifecycle = streams[StreamName.LIFECYCLE]
    text = streams[StreamName.TEXT]
    kind = UpdateKind(lifecycle.choice_index(config.update_kind_weights))
    customer_pick = lifecycle.pick(live_customer_count)
    product_pick = lifecycle.pick(live_product_count)
    country_idx = lifecycle.choice_index(countries.country_weights())
    city_pick = _index(lifecycle.pick(len(table[country_idx].cities)))
    price_cents = lifecycle.between(
        config.prices.list_price_min_cents, config.prices.list_price_max_cents
    )
    category_pick = _index(lifecycle.pick(len(textgen.words("categories"))))
    first_idx = _index(text.pick(len(textgen.words("first_names"))))
    last_idx = _index(text.pick(len(textgen.words("last_names"))))
    adjective_idx = _index(text.pick(len(textgen.words("product_adjectives"))))
    noun_idx = _index(text.pick(len(textgen.words("product_nouns"))))
    return UpdateDraws(
        kind,
        customer_pick,
        product_pick,
        country_idx,
        city_pick,
        price_cents,
        category_pick,
        first_idx,
        last_idx,
        adjective_idx,
        noun_idx,
    )


def apply_update(
    state: EngineState,
    live_customers: list[int],
    live_products: list[int],
    ts_us: int,
    draws: UpdateDraws,
) -> Op | None:
    """Apply one update to the record the draws name and return its op, or None for a no-op.

    The target is resolved by index into the ascending live list. A soft delete or a
    discontinuation removes the id from its live list; nothing else touches the lists.
    """
    if draws.kind in _CUSTOMER_KINDS:
        return _update_customer(state, live_customers, ts_us, draws)
    return _update_product(state, live_products, ts_us, draws)


def _update_customer(
    state: EngineState, live: list[int], ts_us: int, draws: UpdateDraws
) -> Op | None:
    pick = draws.customer_pick
    if pick is None:
        return None
    customer_id = live[pick]
    rec = state.world.customers[customer_id]
    if draws.kind is UpdateKind.CUSTOMER_MOVE:
        # Type 2: the city and the country move together.
        rec.country_idx = draws.country_idx
        rec.city_idx = draws.city_pick
    elif draws.kind is UpdateKind.CUSTOMER_EMAIL:
        # Type 1: the address follows the current name, and the version makes it always new.
        rec.email_first_idx = rec.first_idx
        rec.email_last_idx = rec.last_idx
        rec.email_version += 1
    elif draws.kind is UpdateKind.CUSTOMER_NAME:
        rec.first_idx = draws.first_idx
        rec.last_idx = draws.last_idx
    else:
        rec.deleted_us = ts_us
        del live[pick]
    return Op(
        Table.CUSTOMERS,
        OpKind.UPDATE,
        {"customer_id": customer_id},
        customers.row(rec, ts_us),
    )


def _update_product(
    state: EngineState, live: list[int], ts_us: int, draws: UpdateDraws
) -> Op | None:
    pick = draws.product_pick
    if pick is None:
        return None
    product_id = live[pick]
    rec = state.world.products[product_id]
    if draws.kind is UpdateKind.PRODUCT_NAME:
        rec.adjective_idx = draws.adjective_idx
        rec.noun_idx = draws.noun_idx
    elif draws.kind is UpdateKind.PRODUCT_CATEGORY:
        rec.category_idx = draws.category_pick
    elif draws.kind is UpdateKind.PRODUCT_PRICE:
        rec.list_price_cents = draws.price_cents
    else:
        rec.deleted_us = ts_us
        del live[pick]
    return Op(
        Table.PRODUCTS,
        OpKind.UPDATE,
        {"product_id": product_id},
        products.row(rec, ts_us),
    )
