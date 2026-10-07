"""The model config: seed, simulated range and every volume, rate, horizon and knob, all integers.

RED-stage stub for Plan 02-03: the types and the backfill defaults exist so the tests type-check,
but the strict loader, the validation and the two hashes are the tracer's (three keys) until the
GREEN commit replaces them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from . import canon, clock

SEED_LIMIT = 2**63

_STUB_CANARY = "stub-canary"


class ConfigError(ValueError):
    """The config is not one this generator accepts; the message names a key path."""


class UpdateKind(IntEnum):
    CUSTOMER_MOVE = 0
    CUSTOMER_EMAIL = 1
    CUSTOMER_NAME = 2
    CUSTOMER_SOFT_DELETE = 3
    PRODUCT_NAME = 4
    PRODUCT_CATEGORY = 5
    PRODUCT_PRICE = 6
    PRODUCT_DISCONTINUE = 7


PAYMENT_METHODS = ("card", "paypal", "bank_transfer", "gift_card")


@dataclass(frozen=True)
class Volumes:
    initial_customers: int
    initial_products: int
    customers_per_day: int
    products_per_day: int
    orders_per_day: int
    updates_per_day: int


@dataclass(frozen=True)
class BusinessPpm:
    cancel: int
    line_delete: int
    refund: int
    review: int
    moderation: int


@dataclass(frozen=True)
class OrderShape:
    max_lines: int
    max_quantity: int
    discount_percent_weights: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class PaymentShape:
    method_weights: tuple[int, ...]
    refund_percent_weights: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ReviewShape:
    rating_weights: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Prices:
    list_price_min_cents: int
    list_price_max_cents: int


@dataclass(frozen=True)
class Lifecycle:
    pay_after: tuple[int, int]
    ship_after_pay: tuple[int, int]
    deliver_after_ship: tuple[int, int]
    refund_after_delivery: tuple[int, int]
    review_after_delivery: tuple[int, int]
    moderation_after_review: tuple[int, int]


@dataclass(frozen=True)
class Horizons:
    late_order_max: int
    edit_horizon: int
    late_product_max: int


@dataclass(frozen=True)
class Knobs:
    late_order_ppm: int
    late_product_ppm: int
    duplicate_ppm: int
    out_of_order_ppm: int
    beyond_watermark_ppm: int
    malformed_ppm: int
    hot_product_share_ppm: int
    hot_customer_share_ppm: int
    out_of_order_max_delay_us: int
    beyond_watermark_min_delay_us: int
    beyond_watermark_max_delay_us: int
    schema_drift_at_us: int
    canary_customers: int
    injection_reviews: int


@dataclass(frozen=True)
class ModelConfig:
    canary_token: str
    seed: int
    start_us: int
    end_us: int
    speed: int
    volumes: Volumes
    business_ppm: BusinessPpm
    update_kind_weights: tuple[int, ...]
    orders: OrderShape
    payments: PaymentShape
    reviews: ReviewShape
    prices: Prices
    lifecycle_us: Lifecycle
    horizons: Horizons
    knobs: Knobs

    @classmethod
    def default(cls, canary_token: str) -> ModelConfig:
        """The backfill defaults: 2025, S = 60, D = 24 h, L = 72 h, K = 10 min and ADR-005's rates."""
        hour = clock.US_PER_HOUR
        return cls(
            canary_token=canary_token,
            seed=1,
            start_us=clock.parse("2025-01-01T00:00:00.000000Z"),
            end_us=clock.parse("2026-01-01T00:00:00.000000Z"),
            speed=60,
            volumes=Volumes(500, 120, 100, 2, 1000, 150),
            business_ppm=BusinessPpm(50_000, 30_000, 50_000, 200_000, 20_000),
            update_kind_weights=(3, 3, 3, 1, 2, 2, 2, 1),
            orders=OrderShape(4, 3, ((0, 60), (5, 15), (10, 12), (15, 8), (20, 5))),
            payments=PaymentShape((6, 2, 1, 1), ((25, 1), (50, 1), (100, 2))),
            reviews=ReviewShape(((1, 1), (2, 1), (3, 2), (4, 4), (5, 6))),
            prices=Prices(500, 50_000),
            lifecycle_us=Lifecycle(
                pay_after=(1_000_000, 12 * hour),
                ship_after_pay=(hour, 18 * hour),
                deliver_after_ship=(hour, 16 * hour),
                refund_after_delivery=(hour, 72 * hour),
                review_after_delivery=(hour, 72 * hour),
                moderation_after_review=(hour, 72 * hour),
            ),
            horizons=Horizons(24 * hour, 72 * hour, 10 * clock.US_PER_MINUTE),
            knobs=Knobs(
                late_order_ppm=20_000,
                late_product_ppm=10_000,
                duplicate_ppm=20_000,
                out_of_order_ppm=10_000,
                beyond_watermark_ppm=5_000,
                malformed_ppm=5_000,
                hot_product_share_ppm=200_000,
                hot_customer_share_ppm=200_000,
                out_of_order_max_delay_us=90_000_000,
                beyond_watermark_min_delay_us=900_000_000,
                beyond_watermark_max_delay_us=3_600_000_000,
                schema_drift_at_us=clock.parse("2025-07-01T00:00:00.000000Z"),
                canary_customers=1,
                injection_reviews=1,
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "range": {"start": clock.render(self.start_us), "end": clock.render(self.end_us)},
            "seed": self.seed,
            "volumes": {
                "initial_customers": self.volumes.initial_customers,
                "customers_per_day": self.volumes.customers_per_day,
            },
        }

    def config_sha256(self) -> str:
        return hashlib.sha256(canon.dumps(self.to_json()).encode("utf-8")).hexdigest()

    def model_sha256(self) -> str:
        raise NotImplementedError("RED stub")


def _mapping(obj: object, path: str, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(obj, dict):
        raise ConfigError(f"{path}: expected an object")
    found = set(obj)
    if missing := sorted(keys - found):
        raise ConfigError(f"{path}: missing key {missing[0]}")
    if unknown := sorted(found - keys):
        raise ConfigError(f"{path}: unknown key {unknown[0]}")
    return obj


def _int(value: object, path: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{path}: expected an integer")
    return value


def _time(value: object, path: str) -> int:
    if not isinstance(value, str):
        raise ConfigError(f"{path}: expected a time string")
    try:
        return clock.parse(value)
    except ValueError:
        raise ConfigError(f"{path}: not a YYYY-MM-DDTHH:MM:SS.ffffffZ time") from None


def from_json(obj: object) -> ModelConfig:
    """The tracer's loader: three keys, everything else from the defaults."""
    top = _mapping(obj, "config", {"range", "seed", "volumes"})
    window = _mapping(top["range"], "range", {"start", "end"})
    volumes = _mapping(top["volumes"], "volumes", {"initial_customers", "customers_per_day"})
    seed = _int(top["seed"], "seed")
    if not 0 <= seed < SEED_LIMIT:
        raise ConfigError("seed: must be in [0, 2**63)")
    start_us = _time(window["start"], "range.start")
    end_us = _time(window["end"], "range.end")
    if start_us >= end_us:
        raise ConfigError("range: start must be before end")
    base = ModelConfig.default(canary_token=_STUB_CANARY)
    return dataclasses.replace(
        base,
        seed=seed,
        start_us=start_us,
        end_us=end_us,
        volumes=dataclasses.replace(
            base.volumes,
            initial_customers=_int(volumes["initial_customers"], "volumes.initial_customers"),
            customers_per_day=_int(volumes["customers_per_day"], "volumes.customers_per_day"),
        ),
    )


def _refuse_float(_: str) -> object:
    raise ConfigError("config: a float is not allowed anywhere")


def load(path: Path) -> ModelConfig:
    """Read a UTF-8 JSON config file; a float anywhere in it is an error (D-08)."""
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text, parse_float=_refuse_float, parse_constant=_refuse_float)
    except json.JSONDecodeError:
        raise ConfigError("config: not valid JSON") from None
    return from_json(parsed)
