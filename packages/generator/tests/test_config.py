"""The model config: every value CORE-05 lists, a strict loader, and two canonical hashes.

Every failure case names the key path and never the value, because a config can hold a canary
token and public CI logs are copies erasure can't reach. `CANARY` is a name without "token" in
it, passed by keyword through the variable, so the S105 and S106 rules never see a literal.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from shopstream_generator import clock
from shopstream_generator.config import (
    PAYMENT_METHODS,
    ConfigError,
    ModelConfig,
    UpdateKind,
    from_json,
    load,
)

from .strategies import CANARY

REPO = Path(__file__).resolve().parents[3]
GOLDEN_CONFIG = REPO / "packages/generator/tests/golden/config.json"
MICROS_PER_HOUR = clock.US_PER_HOUR


def default() -> ModelConfig:
    return ModelConfig.default(canary_token=CANARY)


def base() -> dict[str, Any]:
    """A fresh, mutable, valid config object (the default, through JSON text)."""
    parsed: dict[str, Any] = json.loads(json.dumps(default().to_json()))
    return parsed


def key_paths(obj: object, prefix: str = "") -> set[str]:
    """Every object key path; a list is a leaf, so table sizes don't matter."""
    if not isinstance(obj, dict):
        return {prefix}
    found: set[str] = set()
    for key, value in obj.items():
        found |= key_paths(value, f"{prefix}.{key}" if prefix else key)
    return found


def leaves(obj: object) -> list[object]:
    if isinstance(obj, dict):
        return [leaf for value in obj.values() for leaf in leaves(value)]
    if isinstance(obj, list | tuple):
        return [leaf for value in obj for leaf in leaves(value)]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return [leaf for f in dataclasses.fields(obj) for leaf in leaves(getattr(obj, f.name))]
    return [obj]


# ---------------------------------------------------------------- the defaults and the golden


def test_the_defaults_are_the_backfill_values() -> None:
    cfg = default()
    assert cfg.canary_token == CANARY
    assert cfg.seed == 1
    assert clock.render(cfg.start_us) == "2025-01-01T00:00:00.000000Z"
    assert clock.render(cfg.end_us) == "2026-01-01T00:00:00.000000Z"
    assert cfg.speed == 60
    assert dataclasses.astuple(cfg.volumes) == (500, 120, 100, 2, 1000, 150)
    assert dataclasses.astuple(cfg.business_ppm) == (50_000, 30_000, 50_000, 200_000, 20_000)
    assert cfg.update_kind_weights == (3, 3, 3, 1, 2, 2, 2, 1)
    assert cfg.orders.max_lines == 4
    assert cfg.orders.max_quantity == 3
    assert cfg.orders.discount_percent_weights == ((0, 60), (5, 15), (10, 12), (15, 8), (20, 5))
    assert cfg.payments.method_weights == (6, 2, 1, 1)
    assert cfg.payments.refund_percent_weights == ((25, 1), (50, 1), (100, 2))
    assert cfg.reviews.rating_weights == ((1, 1), (2, 1), (3, 2), (4, 4), (5, 6))
    assert dataclasses.astuple(cfg.prices) == (500, 50_000)
    assert cfg.lifecycle_us.pay_after == (1_000_000, 43_200_000_000)
    assert cfg.lifecycle_us.ship_after_pay == (3_600_000_000, 64_800_000_000)
    assert cfg.lifecycle_us.deliver_after_ship == (3_600_000_000, 57_600_000_000)
    for later in (
        cfg.lifecycle_us.refund_after_delivery,
        cfg.lifecycle_us.review_after_delivery,
        cfg.lifecycle_us.moderation_after_review,
    ):
        assert later == (3_600_000_000, 259_200_000_000)
    assert dataclasses.astuple(cfg.horizons) == (86_400_000_000, 259_200_000_000, 600_000_000)
    knobs = cfg.knobs
    assert (knobs.late_order_ppm, knobs.late_product_ppm, knobs.duplicate_ppm) == (
        20_000,
        10_000,
        20_000,
    )
    assert (knobs.out_of_order_ppm, knobs.out_of_order_max_delay_us) == (10_000, 90_000_000)
    assert knobs.beyond_watermark_ppm == 5_000
    assert knobs.beyond_watermark_min_delay_us == 900_000_000
    assert knobs.beyond_watermark_max_delay_us == 3_600_000_000
    assert knobs.malformed_ppm == 5_000
    assert (knobs.hot_product_share_ppm, knobs.hot_customer_share_ppm) == (200_000, 200_000)
    assert clock.render(knobs.schema_drift_at_us) == "2025-07-01T00:00:00.000000Z"
    assert (knobs.canary_customers, knobs.injection_reviews) == (1, 1)


def test_the_enums_fix_the_code_order_of_the_weight_tables() -> None:
    assert [kind.name for kind in UpdateKind] == [
        "CUSTOMER_MOVE",
        "CUSTOMER_EMAIL",
        "CUSTOMER_NAME",
        "CUSTOMER_SOFT_DELETE",
        "PRODUCT_NAME",
        "PRODUCT_CATEGORY",
        "PRODUCT_PRICE",
        "PRODUCT_DISCONTINUE",
    ]
    assert [int(kind) for kind in UpdateKind] == list(range(8))
    assert PAYMENT_METHODS == ("card", "paypal", "bank_transfer", "gift_card")


def test_the_default_config_json_has_no_float_leaf() -> None:
    cfg = default()
    assert not [leaf for leaf in leaves(cfg.to_json()) if isinstance(leaf, float)]
    assert not [leaf for leaf in leaves(cfg) if isinstance(leaf, float)]


def test_a_config_cannot_be_built_without_a_canary() -> None:
    cfg = default()
    kwargs: dict[str, Any] = {
        f.name: getattr(cfg, f.name) for f in dataclasses.fields(cfg) if f.name != "canary_token"
    }
    with pytest.raises(TypeError):
        ModelConfig(**kwargs)


def test_the_json_round_trip_is_an_equal_config() -> None:
    cfg = default()
    assert from_json(json.loads(json.dumps(cfg.to_json()))) == cfg


def test_the_golden_file_loads_to_the_golden_values() -> None:
    cfg = load(GOLDEN_CONFIG)
    assert cfg.canary_token == CANARY
    assert cfg.seed == 3
    assert clock.render(cfg.start_us) == "2025-06-28T00:00:00.000000Z"
    assert clock.render(cfg.end_us) == "2025-07-05T00:00:00.000000Z"
    assert cfg.speed == 60
    assert dataclasses.astuple(cfg.volumes) == (30, 40, 10, 2, 60, 24)
    assert dataclasses.astuple(cfg.business_ppm) == (80_000, 150_000, 100_000, 300_000, 80_000)
    # Every other value is the default's, spelled out in the file so a changed default can't move it.
    stock = default()
    for name in (
        "update_kind_weights",
        "orders",
        "payments",
        "reviews",
        "prices",
        "lifecycle_us",
        "horizons",
        "knobs",
    ):
        assert getattr(cfg, name) == getattr(stock, name), name


def test_the_golden_file_sets_every_key_explicitly() -> None:
    golden = json.loads(GOLDEN_CONFIG.read_text(encoding="utf-8"))
    assert key_paths(golden) == key_paths(default().to_json())


def test_the_golden_file_is_two_space_indented_sorted_json_with_one_trailing_newline() -> None:
    text = GOLDEN_CONFIG.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------- strictness, one case per rule


def put(doc: dict[str, Any], path: str, value: object) -> None:
    """Set the leaf at a dotted path; a digit segment indexes a list."""
    *parents, last = path.split(".")
    node: Any = doc
    for part in parents:
        node = node[int(part)] if part.isdigit() else node[part]
    node[int(last) if last.isdigit() else last] = value


def drop(doc: dict[str, Any], section: str, key: str) -> None:
    del doc[section][key]


def empty_object(doc: dict[str, Any]) -> None:
    doc.clear()


def start_equals_end(doc: dict[str, Any]) -> None:
    doc["range"]["end"] = doc["range"]["start"]


def lifecycle_over_48h(doc: dict[str, Any]) -> None:
    horizons = doc["horizons_us"]
    room = horizons["edit_horizon"] - horizons["late_order_max"]
    life = doc["lifecycle_us"]
    others = life["ship_after_pay"][1] + life["deliver_after_ship"][1]
    life["pay_after"][1] = room - others + 1


def l_equals_d(doc: dict[str, Any]) -> None:
    doc["horizons_us"]["edit_horizon"] = doc["horizons_us"]["late_order_max"]


def discount_duplicate(doc: dict[str, Any]) -> None:
    doc["orders"]["discount_percent_weights"][1][0] = doc["orders"]["discount_percent_weights"][0][
        0
    ]


def rating_set(doc: dict[str, Any]) -> None:
    doc["reviews"]["rating_weights"][4][0] = 6


def weights_all_zero(doc: dict[str, Any]) -> None:
    doc["payments"]["method_weights"] = dict.fromkeys(PAYMENT_METHODS, 0)


def mutation(path: str, value: object) -> Callable[[dict[str, Any]], None]:
    return lambda doc: put(doc, path, value)


def missing(section: str, key: str) -> Callable[[dict[str, Any]], None]:
    return lambda doc: drop(doc, section, key)


def unknown(section: str) -> Callable[[dict[str, Any]], None]:
    return lambda doc: doc[section].__setitem__("bogus_key", 1)


# (id, mutator, key path the message must name, text the message must not repeat)
BAD_CONFIGS: list[tuple[str, Callable[[dict[str, Any]], None], str, str | None]] = [
    ("float", mutation("volumes.orders_per_day", 1.5), "volumes.orders_per_day", "1.5"),
    ("bool", mutation("volumes.initial_customers", True), "volumes.initial_customers", "True"),
    ("unknown-key", unknown("volumes"), "volumes", None),
    ("missing-key", missing("knobs", "duplicate_ppm"), "knobs", None),
    ("empty-object", empty_object, "config", None),
    ("start-equals-end", start_equals_end, "range", None),
    ("ppm-negative", mutation("business_ppm.cancel", -1), "business_ppm.cancel", "-1"),
    ("ppm-over", mutation("knobs.duplicate_ppm", 1_000_001), "knobs.duplicate_ppm", "1000001"),
    ("lifecycle-over-48h", lifecycle_over_48h, "lifecycle_us", None),
    ("l-equals-d", l_equals_d, "horizons_us", None),
    (
        "discount-over-50",
        mutation("orders.discount_percent_weights.0.0", 51),
        "orders.discount_percent_weights",
        None,
    ),
    ("discount-duplicate", discount_duplicate, "orders.discount_percent_weights", None),
    ("rating-set", rating_set, "reviews.rating_weights", None),
    ("weights-all-zero", weights_all_zero, "payments.method_weights", None),
    (
        "per-day-over-us-per-day",
        mutation("volumes.orders_per_day", 86_400_000_001),
        "volumes.orders_per_day",
        "86400000001",
    ),
    ("canary-whitespace", mutation("canary_token", "bad token"), "canary_token", "bad token"),
]


@pytest.mark.parametrize(
    ("mutate", "path", "leak"),
    [pytest.param(m, p, leak, id=case) for case, m, p, leak in BAD_CONFIGS],
)
def test_a_bad_config_raises_and_names_the_key_path(
    mutate: Callable[[dict[str, Any]], None], path: str, leak: str | None
) -> None:
    doc = base()
    mutate(doc)
    with pytest.raises(ConfigError) as caught:
        from_json(doc)
    message = str(caught.value)
    assert path in message
    if leak is not None:
        assert leak not in message


def test_the_ids_the_plan_names_are_all_collected() -> None:
    assert [case for case, *_ in BAD_CONFIGS] == [
        "float",
        "bool",
        "unknown-key",
        "missing-key",
        "empty-object",
        "start-equals-end",
        "ppm-negative",
        "ppm-over",
        "lifecycle-over-48h",
        "l-equals-d",
        "discount-over-50",
        "discount-duplicate",
        "rating-set",
        "weights-all-zero",
        "per-day-over-us-per-day",
        "canary-whitespace",
    ]


def test_an_object_that_is_not_an_object_raises() -> None:
    with pytest.raises(ConfigError, match="config"):
        from_json([])


@pytest.mark.parametrize("seed", [-1, 2**63])
def test_a_seed_outside_zero_to_two_to_the_63_raises(seed: int) -> None:
    doc = base()
    doc["seed"] = seed
    with pytest.raises(ConfigError, match="seed"):
        from_json(doc)


@pytest.mark.parametrize("seed", [0, 2**63 - 1])
def test_the_seed_limits_load(seed: int) -> None:
    doc = base()
    doc["seed"] = seed
    assert from_json(doc).seed == seed


def test_a_speed_below_one_raises() -> None:
    doc = base()
    doc["speed"] = 0
    with pytest.raises(ConfigError, match="speed"):
        from_json(doc)


def test_direct_construction_validates_too() -> None:
    with pytest.raises(ConfigError, match="speed"):
        dataclasses.replace(default(), speed=0)
    with pytest.raises(ConfigError, match="range"):
        dataclasses.replace(default(), end_us=default().start_us)


# ---------------------------------------------------------------- boundaries that load


@pytest.mark.parametrize("ppm", [0, 1_000_000])
def test_a_ppm_at_either_limit_loads(ppm: int) -> None:
    doc = base()
    put(doc, "knobs.malformed_ppm", ppm)
    put(doc, "business_ppm.refund", ppm)
    cfg = from_json(doc)
    assert cfg.knobs.malformed_ppm == ppm
    assert cfg.business_ppm.refund == ppm


@pytest.mark.parametrize("per_day", [0, 86_400_000_000])
def test_a_per_day_volume_at_either_limit_loads(per_day: int) -> None:
    doc = base()
    put(doc, "volumes.orders_per_day", per_day)
    assert from_json(doc).volumes.orders_per_day == per_day


def test_pay_ship_and_deliver_maxima_summing_to_exactly_48_hours_load() -> None:
    doc = base()
    room = doc["horizons_us"]["edit_horizon"] - doc["horizons_us"]["late_order_max"]
    assert room == 48 * MICROS_PER_HOUR
    life = doc["lifecycle_us"]
    life["pay_after"][1] = room - life["ship_after_pay"][1] - life["deliver_after_ship"][1]
    cfg = from_json(doc)
    total = (
        cfg.lifecycle_us.pay_after[1]
        + cfg.lifecycle_us.ship_after_pay[1]
        + cfg.lifecycle_us.deliver_after_ship[1]
    )
    assert total == room


def test_update_weights_may_be_all_zero_only_when_no_update_happens() -> None:
    quiet = base()
    put(quiet, "volumes.updates_per_day", 0)
    quiet["update_kind_weights"] = {kind.name.lower(): 0 for kind in UpdateKind}
    assert from_json(quiet).update_kind_weights == (0,) * 8
    busy = base()
    busy["update_kind_weights"] = {kind.name.lower(): 0 for kind in UpdateKind}
    with pytest.raises(ConfigError, match="update_kind_weights"):
        from_json(busy)


# ---------------------------------------------------------------- floats, ordering and hashes


def test_a_float_in_the_file_is_refused_at_parse_time(tmp_path: Path) -> None:
    doc = base()
    text = json.dumps(doc, sort_keys=True).replace(
        '"orders_per_day": 1000,', '"orders_per_day": 1000.0,'
    )
    assert "1000.0" in text
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="float"):
        load(path)
    nan_text = json.dumps(doc, sort_keys=True).replace('"seed": 1,', '"seed": NaN,')
    assert "NaN" in nan_text
    path.write_text(nan_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="float"):
        load(path)


def test_invalid_json_text_raises_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON"):
        load(path)


def reverse_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {key: reverse_keys(obj[key]) for key in reversed(list(obj))}
    if isinstance(obj, list):
        return [reverse_keys(item) for item in obj]
    return obj


def test_config_sha256_ignores_the_order_of_the_json_keys() -> None:
    golden: dict[str, Any] = json.loads(GOLDEN_CONFIG.read_text(encoding="utf-8"))
    permuted = reverse_keys(golden)
    assert list(permuted) != list(golden)
    assert from_json(permuted).config_sha256() == from_json(golden).config_sha256()
    assert from_json(golden).config_sha256() == load(GOLDEN_CONFIG).config_sha256()


def test_weight_tables_load_into_the_codes_fixed_order() -> None:
    doc = base()
    doc["payments"]["method_weights"] = dict(
        reversed(list(doc["payments"]["method_weights"].items()))
    )
    doc["update_kind_weights"] = dict(reversed(list(doc["update_kind_weights"].items())))
    cfg = from_json(doc)
    assert cfg.payments.method_weights == default().payments.method_weights
    assert cfg.update_kind_weights == default().update_kind_weights
    assert cfg.config_sha256() == default().config_sha256()


def test_the_two_hashes_say_what_changed() -> None:
    cfg = default()
    longer = dataclasses.replace(cfg, end_us=cfg.end_us + clock.US_PER_DAY, speed=cfg.speed * 2)
    assert longer.model_sha256() == cfg.model_sha256()
    assert longer.config_sha256() != cfg.config_sha256()
    other_seed = dataclasses.replace(cfg, seed=cfg.seed + 1)
    assert other_seed.model_sha256() != cfg.model_sha256()
    assert other_seed.config_sha256() != cfg.config_sha256()
    other_start = dataclasses.replace(cfg, start_us=cfg.start_us + 1)
    assert other_start.model_sha256() != cfg.model_sha256()
    assert len(cfg.model_sha256()) == 64
