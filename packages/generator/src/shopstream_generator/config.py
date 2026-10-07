"""The model config: every volume, rate, horizon and knob the generator reads (D-08, D-15, D-22).

A `ModelConfig` is a tree of frozen dataclasses that hold ints, strings and tuples of ints, and
nothing else: no float enters, and a `bool` is not an int. It is validated where it is built
(`__post_init__`), so a config made in code obeys the same rules as one loaded from JSON. Every
JSON key is mandatory and an unknown key is an error, so a typo can't silently fall back to a
default. Times are written and read in the one canonical form `YYYY-MM-DDTHH:MM:SS.ffffffZ`,
durations are integer microseconds.

`ModelConfig.default(canary_token)` is the backfill: 2025, S = 60, D = 24 h, L = 72 h, K = 10 min
and ADR-005's default knob rates. The canary token has no default and no committed name holds a
literal, so S105 and S106 never fire and a real value can't be committed by accident.

`ConfigError` names the key path and never the value: a config can hold the canary token, and
public CI logs are copies erasure can't reach.

Two hashes: `config_sha256` over the whole canonical config (the manifest records it), and
`model_sha256` over the config without the range end and S, which is what a resumed run must
match: a run may be extended or re-paced, never re-seeded or re-shaped.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from . import canon, clock
from .rng import PPM

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

SEED_LIMIT = 2**63


class ConfigError(ValueError):
    """The config is not one this generator accepts; the message names a key path."""


class UpdateKind(IntEnum):
    """The in-life update kinds; the weight table loads in this order, whatever the JSON order."""

    CUSTOMER_MOVE = 0
    CUSTOMER_EMAIL = 1
    CUSTOMER_NAME = 2
    CUSTOMER_SOFT_DELETE = 3
    PRODUCT_NAME = 4
    PRODUCT_CATEGORY = 5
    PRODUCT_PRICE = 6
    PRODUCT_DISCONTINUE = 7


PAYMENT_METHODS = ("card", "paypal", "bank_transfer", "gift_card")

_UPDATE_KIND_NAMES = tuple(kind.name.lower() for kind in UpdateKind)

_MAX_PERCENT_DISCOUNT = 50
_MAX_PERCENT = 100
_RATINGS = (1, 2, 3, 4, 5)


# ---------------------------------------------------------------- validation helpers


def _int_in(value: object, path: str, lo: int, hi: int | None = None) -> int:
    """`value` as an int in [lo, hi] (no upper bound when `hi` is None); never a bool or a float."""
    if type(value) is not int:
        raise ConfigError(f"{path}: expected an integer")
    if value < lo or (hi is not None and value > hi):
        bound = f"in [{lo}, {hi}]" if hi is not None else f"at least {lo}"
        raise ConfigError(f"{path}: must be {bound}")
    return value


def _ppm(value: object, path: str) -> int:
    return _int_in(value, path, 0, PPM)


def _weights(value: object, path: str, size: int, *, positive: bool) -> tuple[int, ...]:
    """A tuple of `size` non-negative ints, with a positive sum when `positive`."""
    if not isinstance(value, tuple) or len(value) != size:
        raise ConfigError(f"{path}: expected {size} weights")
    total = 0
    for index, weight in enumerate(value):
        total += _int_in(weight, f"{path}[{index}]", 0)
    if positive and total <= 0:
        raise ConfigError(f"{path}: the weights must have a positive sum")
    return value


def _pairs(value: object, path: str, lo: int, hi: int) -> tuple[tuple[int, int], ...]:
    """A non-empty table of (key, weight): keys unique in [lo, hi], weights >= 0, sum > 0."""
    if not isinstance(value, tuple) or not value:
        raise ConfigError(f"{path}: expected a non-empty table of [key, weight] pairs")
    seen: set[int] = set()
    total = 0
    for index, pair in enumerate(value):
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ConfigError(f"{path}[{index}]: expected a [key, weight] pair")
        key = _int_in(pair[0], f"{path}[{index}][0]", lo, hi)
        total += _int_in(pair[1], f"{path}[{index}][1]", 0)
        if key in seen:
            raise ConfigError(f"{path}[{index}][0]: a key is listed twice")
        seen.add(key)
    if total <= 0:
        raise ConfigError(f"{path}: the weights must have a positive sum")
    return value


def _span(value: object, path: str) -> tuple[int, int]:
    """A (lo, hi) pair of microsecond durations with 1 <= lo <= hi."""
    if not isinstance(value, tuple) or len(value) != 2:
        raise ConfigError(f"{path}: expected a [lo, hi] pair")
    lo = _int_in(value[0], f"{path}[0]", 1)
    hi = _int_in(value[1], f"{path}[1]", 1)
    if hi < lo:
        raise ConfigError(f"{path}: lo must not exceed hi")
    return lo, hi


# ---------------------------------------------------------------- the config tree


@dataclass(frozen=True)
class Volumes:
    initial_customers: int
    initial_products: int
    customers_per_day: int
    products_per_day: int
    orders_per_day: int
    updates_per_day: int

    def __post_init__(self) -> None:
        _int_in(self.initial_customers, "volumes.initial_customers", 0)
        _int_in(self.initial_products, "volumes.initial_products", 0)
        for name in ("customers_per_day", "products_per_day", "orders_per_day", "updates_per_day"):
            _int_in(getattr(self, name), f"volumes.{name}", 0, clock.US_PER_DAY)


@dataclass(frozen=True)
class BusinessPpm:
    cancel: int
    line_delete: int
    refund: int
    review: int
    moderation: int

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            _ppm(getattr(self, field.name), f"business_ppm.{field.name}")


@dataclass(frozen=True)
class OrderShape:
    max_lines: int
    max_quantity: int
    discount_percent_weights: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        _int_in(self.max_lines, "orders.max_lines", 1)
        _int_in(self.max_quantity, "orders.max_quantity", 1)
        _pairs(
            self.discount_percent_weights,
            "orders.discount_percent_weights",
            0,
            _MAX_PERCENT_DISCOUNT,
        )


@dataclass(frozen=True)
class PaymentShape:
    method_weights: tuple[int, ...]
    refund_percent_weights: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        _weights(
            self.method_weights, "payments.method_weights", len(PAYMENT_METHODS), positive=True
        )
        _pairs(self.refund_percent_weights, "payments.refund_percent_weights", 1, _MAX_PERCENT)


@dataclass(frozen=True)
class ReviewShape:
    rating_weights: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        table = _pairs(self.rating_weights, "reviews.rating_weights", 1, len(_RATINGS))
        if tuple(rating for rating, _ in table) != _RATINGS:
            raise ConfigError("reviews.rating_weights: ratings must be exactly 1 to 5, in order")


@dataclass(frozen=True)
class Prices:
    list_price_min_cents: int
    list_price_max_cents: int

    def __post_init__(self) -> None:
        low = _int_in(self.list_price_min_cents, "prices.list_price_min_cents", 1)
        _int_in(self.list_price_max_cents, "prices.list_price_max_cents", low)


@dataclass(frozen=True)
class Lifecycle:
    pay_after: tuple[int, int]
    ship_after_pay: tuple[int, int]
    deliver_after_ship: tuple[int, int]
    refund_after_delivery: tuple[int, int]
    review_after_delivery: tuple[int, int]
    moderation_after_review: tuple[int, int]

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            _span(getattr(self, field.name), f"lifecycle_us.{field.name}")


@dataclass(frozen=True)
class Horizons:
    """D, L and K: the late-order bound, the edit horizon and the late-product delay."""

    late_order_max: int
    edit_horizon: int
    late_product_max: int

    def __post_init__(self) -> None:
        late = _int_in(self.late_order_max, "horizons_us.late_order_max", 1)
        edit = _int_in(self.edit_horizon, "horizons_us.edit_horizon", 1)
        _int_in(self.late_product_max, "horizons_us.late_product_max", 1)
        if edit <= late:
            raise ConfigError("horizons_us.edit_horizon: must exceed late_order_max")


_KNOB_PPM = (
    "late_order_ppm",
    "late_product_ppm",
    "duplicate_ppm",
    "out_of_order_ppm",
    "beyond_watermark_ppm",
    "malformed_ppm",
    "hot_product_share_ppm",
    "hot_customer_share_ppm",
)


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

    def __post_init__(self) -> None:
        for name in _KNOB_PPM:
            _ppm(getattr(self, name), f"knobs.{name}")
        _int_in(self.out_of_order_max_delay_us, "knobs.out_of_order_max_delay_us", 1)
        low = _int_in(self.beyond_watermark_min_delay_us, "knobs.beyond_watermark_min_delay_us", 1)
        _int_in(self.beyond_watermark_max_delay_us, "knobs.beyond_watermark_max_delay_us", low)
        _int_in(self.schema_drift_at_us, "knobs.schema_drift_at", 0)
        _int_in(self.canary_customers, "knobs.canary_customers", 0)
        _int_in(self.injection_reviews, "knobs.injection_reviews", 0)


@dataclass(frozen=True)
class ModelConfig:
    """Everything a run reads. The canary token comes first and has no default."""

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

    def __post_init__(self) -> None:
        token = self.canary_token
        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or not token.isprintable()
            or any(char.isspace() for char in token)
        ):
            raise ConfigError("canary_token: expected non-empty printable ASCII without whitespace")
        _int_in(self.seed, "seed", 0, SEED_LIMIT - 1)
        start = _int_in(self.start_us, "range.start", 0)
        end = _int_in(self.end_us, "range.end", 0)
        if start >= end:
            raise ConfigError("range: start must be before end")
        _int_in(self.speed, "speed", 1)
        _weights(
            self.update_kind_weights,
            "update_kind_weights",
            len(UpdateKind),
            positive=self.volumes.updates_per_day > 0,
        )
        room = self.horizons.edit_horizon - self.horizons.late_order_max
        life = self.lifecycle_us
        if life.pay_after[1] + life.ship_after_pay[1] + life.deliver_after_ship[1] > room:
            raise ConfigError(
                "lifecycle_us: the pay, ship and deliver maxima must sum to at most "
                "edit_horizon minus late_order_max"
            )

    @classmethod
    def default(cls, canary_token: str) -> ModelConfig:
        """The backfill defaults: 2025, S = 60, D = 24 h, L = 72 h, K = 10 min, ADR-005's rates."""
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

    def _json(self, *, whole: bool) -> dict[str, object]:
        """The canonical JSON shape; `whole=False` leaves out the range end and S."""
        window: dict[str, object] = {"start": clock.render(self.start_us)}
        knobs = {
            key: value for key, value in _flat(self.knobs).items() if key != "schema_drift_at_us"
        }
        knobs["schema_drift_at"] = clock.render(self.knobs.schema_drift_at_us)
        body: dict[str, object] = {
            "business_ppm": _flat(self.business_ppm),
            "canary_token": self.canary_token,
            "horizons_us": _flat(self.horizons),
            "knobs": knobs,
            "lifecycle_us": _flat(self.lifecycle_us),
            "orders": {
                "discount_percent_weights": _plain(self.orders.discount_percent_weights),
                "max_lines": self.orders.max_lines,
                "max_quantity": self.orders.max_quantity,
            },
            "payments": {
                "method_weights": dict(
                    zip(PAYMENT_METHODS, self.payments.method_weights, strict=True)
                ),
                "refund_percent_weights": _plain(self.payments.refund_percent_weights),
            },
            "prices": _flat(self.prices),
            "range": window,
            "reviews": {"rating_weights": _plain(self.reviews.rating_weights)},
            "seed": self.seed,
            "update_kind_weights": dict(
                zip(_UPDATE_KIND_NAMES, self.update_kind_weights, strict=True)
            ),
            "volumes": _flat(self.volumes),
        }
        if whole:
            window["end"] = clock.render(self.end_us)
            body["speed"] = self.speed
        return body

    def to_json(self) -> dict[str, object]:
        """The whole config as JSON-compatible ints, strings, lists and string-keyed objects."""
        return self._json(whole=True)

    def config_sha256(self) -> str:
        """SHA-256 of the canonical config JSON (recorded in the manifest)."""
        return _digest(self.to_json())

    def model_sha256(self) -> str:
        """The same over the config minus the range end and S: the resume guard (D-24)."""
        return _digest(self._json(whole=False))


def _digest(body: dict[str, object]) -> str:
    return hashlib.sha256(canon.dumps(body).encode("utf-8")).hexdigest()


def _plain(value: object) -> object:
    """Tuples become lists, recursively; everything else is already JSON-compatible."""
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _flat(section: DataclassInstance) -> dict[str, object]:
    return {f.name: _plain(getattr(section, f.name)) for f in dataclasses.fields(section)}


# ---------------------------------------------------------------- the strict loader


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


def _ints(obj: object, path: str, names: tuple[str, ...]) -> dict[str, int]:
    section = _mapping(obj, path, set(names))
    return {name: _int(section[name], f"{path}.{name}") for name in names}


def _list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigError(f"{path}: expected a list")
    return value


def _json_pair(value: object, path: str) -> tuple[int, int]:
    items = _list(value, path)
    if len(items) != 2:
        raise ConfigError(f"{path}: expected a pair")
    return _int(items[0], f"{path}[0]"), _int(items[1], f"{path}[1]")


def _json_pairs(value: object, path: str) -> tuple[tuple[int, int], ...]:
    return tuple(
        _json_pair(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path))
    )


def _named_weights(obj: object, path: str, names: tuple[str, ...]) -> tuple[int, ...]:
    """A weight object loaded into `names` order, whatever order the JSON listed its keys."""
    values = _ints(obj, path, names)
    return tuple(values[name] for name in names)


def _names(cls: type[DataclassInstance]) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


def from_json(obj: object) -> ModelConfig:
    """Build a ModelConfig from parsed JSON, or raise ConfigError naming a key path."""
    top = _mapping(
        obj,
        "config",
        {
            "business_ppm",
            "canary_token",
            "horizons_us",
            "knobs",
            "lifecycle_us",
            "orders",
            "payments",
            "prices",
            "range",
            "reviews",
            "seed",
            "speed",
            "update_kind_weights",
            "volumes",
        },
    )
    canary = top["canary_token"]
    if not isinstance(canary, str):
        raise ConfigError("canary_token: expected a string")
    window = _mapping(top["range"], "range", {"start", "end"})

    orders = _mapping(
        top["orders"], "orders", {"max_lines", "max_quantity", "discount_percent_weights"}
    )
    payments = _mapping(top["payments"], "payments", {"method_weights", "refund_percent_weights"})
    reviews = _mapping(top["reviews"], "reviews", {"rating_weights"})

    lifecycle = _mapping(top["lifecycle_us"], "lifecycle_us", set(_names(Lifecycle)))
    knob_names = tuple(name for name in _names(Knobs) if name != "schema_drift_at_us")
    knobs = _mapping(top["knobs"], "knobs", {*knob_names, "schema_drift_at"})
    knob_ints = {name: _int(knobs[name], f"knobs.{name}") for name in knob_names}
    drift = _time(knobs["schema_drift_at"], "knobs.schema_drift_at")

    return ModelConfig(
        canary_token=canary,
        seed=_int(top["seed"], "seed"),
        start_us=_time(window["start"], "range.start"),
        end_us=_time(window["end"], "range.end"),
        speed=_int(top["speed"], "speed"),
        volumes=Volumes(**_ints(top["volumes"], "volumes", _names(Volumes))),
        business_ppm=BusinessPpm(**_ints(top["business_ppm"], "business_ppm", _names(BusinessPpm))),
        update_kind_weights=_named_weights(
            top["update_kind_weights"], "update_kind_weights", _UPDATE_KIND_NAMES
        ),
        orders=OrderShape(
            max_lines=_int(orders["max_lines"], "orders.max_lines"),
            max_quantity=_int(orders["max_quantity"], "orders.max_quantity"),
            discount_percent_weights=_json_pairs(
                orders["discount_percent_weights"], "orders.discount_percent_weights"
            ),
        ),
        payments=PaymentShape(
            method_weights=_named_weights(
                payments["method_weights"], "payments.method_weights", PAYMENT_METHODS
            ),
            refund_percent_weights=_json_pairs(
                payments["refund_percent_weights"], "payments.refund_percent_weights"
            ),
        ),
        reviews=ReviewShape(
            rating_weights=_json_pairs(reviews["rating_weights"], "reviews.rating_weights")
        ),
        prices=Prices(**_ints(top["prices"], "prices", _names(Prices))),
        lifecycle_us=Lifecycle(
            **{
                name: _json_pair(lifecycle[name], f"lifecycle_us.{name}")
                for name in _names(Lifecycle)
            }
        ),
        horizons=Horizons(**_ints(top["horizons_us"], "horizons_us", _names(Horizons))),
        knobs=Knobs(schema_drift_at_us=drift, **knob_ints),
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
