"""In-life updates: stub, replaced by the real process in the next commit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..config import ModelConfig, UpdateKind
from ..ops import Op
from ..rng import Stream, StreamName
from ..state import EngineState


@dataclass(frozen=True)
class UpdateDraws:
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


def draw_update(
    streams: Mapping[StreamName, Stream],
    config: ModelConfig,
    live_customer_count: int,
    live_product_count: int,
) -> UpdateDraws:
    raise NotImplementedError


def apply_update(
    state: EngineState,
    live_customers: list[int],
    live_products: list[int],
    ts_us: int,
    draws: UpdateDraws,
) -> Op | None:
    raise NotImplementedError
