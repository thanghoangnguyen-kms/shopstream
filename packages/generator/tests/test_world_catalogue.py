"""The catalogue: customers (and, from Task 2, products) carry their full REF section 7 columns.

Every case runs the golden config through a real `Engine`, so a row here is the row a sink will
see. Names, cities and emails come only from the committed word lists and the country table
(D-18), and the email uses the reserved example.com domain (RFC 2606, T-02-12).
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from shopstream_generator import clock, countries, golden, textgen
from shopstream_generator import config as model_config
from shopstream_generator.config import ModelConfig
from shopstream_generator.engine import Engine
from shopstream_generator.manifest import EMPTY_SHA256
from shopstream_generator.ops import Op, OpKind, Table, Tick
from shopstream_generator.rng import Stream, StreamName

from .harness import run_ticks

GOLDEN_CONFIG = Path(__file__).resolve().parent / "golden" / "config.json"
CUSTOMER_COLUMNS = {
    "customer_id",
    "email",
    "full_name",
    "city",
    "country",
    "deleted_at",
    "created_at",
    "updated_at",
}
EMAIL = re.compile(r"^[a-z]+\.[a-z]+\.([0-9]+)@example\.com$")


def golden_config() -> ModelConfig:
    return model_config.load(GOLDEN_CONFIG)


def ops_of(ticks: list[Tick], table: Table) -> list[tuple[int, Op]]:
    """`(ts_us, op)` for every op on one table, in emission order."""
    return [(tick.ts_us, op) for tick in ticks for op in tick.ops if op.table is table]


def customer_inserts(ticks: list[Tick]) -> list[tuple[int, Op]]:
    return [(ts, op) for ts, op in ops_of(ticks, Table.CUSTOMERS) if op.kind is OpKind.INSERT]


def row_of(op: Op) -> dict[str, object]:
    assert op.row is not None
    return dict(op.row)


def test_the_first_initial_customers_are_inserted_after_the_initial_products() -> None:
    config = golden_config()
    inserts = customer_inserts(run_ticks(config))
    initial = config.volumes.initial_customers
    stamps = [ts for ts, _ in inserts[:initial]]
    expected = [config.start_us + config.volumes.initial_products + j for j in range(initial)]
    assert stamps == expected


def test_a_sign_up_follows_the_initial_base_with_an_integer_gap() -> None:
    config = golden_config()
    inserts = customer_inserts(run_ticks(config))
    assert len(inserts) > config.volumes.initial_customers
    first_arrival = inserts[config.volumes.initial_customers][0]
    assert (
        first_arrival
        > config.start_us + config.volumes.initial_products + config.volumes.initial_customers
    )


def test_every_customers_row_has_exactly_the_eight_columns() -> None:
    for _, op in customer_inserts(run_ticks(golden_config())):
        assert set(row_of(op)) == CUSTOMER_COLUMNS


def test_every_email_uses_example_com_with_the_rows_own_customer_id() -> None:
    for _, op in customer_inserts(run_ticks(golden_config())):
        row = row_of(op)
        match = EMAIL.fullmatch(str(row["email"]))
        assert match is not None
        assert int(match.group(1)) == row["customer_id"]


def test_customer_ids_run_one_two_three_with_no_gap() -> None:
    ids = [op.key["customer_id"] for _, op in customer_inserts(run_ticks(golden_config()))]
    assert ids == list(range(1, len(ids) + 1))


def test_a_rows_country_and_city_come_from_the_same_table_row() -> None:
    pairs = {(c.code, city) for c in countries.countries() for city in c.cities}
    for _, op in customer_inserts(run_ticks(golden_config())):
        row = row_of(op)
        assert (row["country"], row["city"]) in pairs


def test_a_full_name_is_a_first_and_a_last_name_from_the_lists() -> None:
    firsts, lasts = set(textgen.words("first_names")), set(textgen.words("last_names"))
    for _, op in customer_inserts(run_ticks(golden_config())):
        first, last = str(row_of(op)["full_name"]).split(" ")
        assert first in firsts
        assert last in lasts


def test_an_insert_row_has_equal_created_and_updated_times_and_no_deletion() -> None:
    for ts, op in customer_inserts(run_ticks(golden_config())):
        row = row_of(op)
        moment = clock.to_datetime(ts)
        assert row["created_at"] == moment
        assert row["updated_at"] == moment
        assert row["deleted_at"] is None
        assert isinstance(row["created_at"], datetime)


def test_the_first_customer_follows_the_draw_table() -> None:
    config = golden_config()
    customers = Stream(config.seed, StreamName.CUSTOMERS)
    text = Stream(config.seed, StreamName.TEXT)
    # `Engine.new` draws the first sign-up's gap before any tick runs.
    mean_gap = clock.US_PER_DAY // config.volumes.customers_per_day
    customers.between(1, 2 * mean_gap)
    table = countries.countries()
    country = table[customers.choice_index(countries.country_weights())]
    city_pick = customers.pick(len(country.cities))
    first_pick = text.pick(len(textgen.words("first_names")))
    last_pick = text.pick(len(textgen.words("last_names")))
    assert city_pick is not None
    assert first_pick is not None
    assert last_pick is not None
    row = row_of(customer_inserts(run_ticks(config))[0][1])
    assert row["country"] == country.code
    assert row["city"] == country.cities[city_pick]
    first = textgen.words("first_names")[first_pick]
    last = textgen.words("last_names")[last_pick]
    assert row["full_name"] == f"{first} {last}"
    assert row["email"] == f"{first}.{last}.1@example.com".lower()


def test_the_world_records_hold_only_ints_and_none() -> None:
    config = golden_config()
    engine = Engine.new(config)
    list(engine.run_until(config.end_us))
    records = engine.state.world.customers
    assert len(records) > config.volumes.initial_customers
    for record in records.values():
        for value in vars(record).values():
            assert value is None or type(value) is int


PRODUCT_COLUMNS = {
    "product_id",
    "name",
    "category",
    "list_price",
    "deleted_at",
    "created_at",
    "updated_at",
}


def product_inserts(ticks: list[Tick]) -> list[tuple[int, Op]]:
    return [(ts, op) for ts, op in ops_of(ticks, Table.PRODUCTS) if op.kind is OpKind.INSERT]


def golden_product_inserts() -> list[tuple[int, Op]]:
    """The golden run's product inserts; it must hold some, so no check below passes vacuously."""
    inserts = product_inserts(run_ticks(golden_config()))
    assert inserts
    return inserts


def test_the_first_initial_products_ticks_are_product_inserts_one_microsecond_apart() -> None:
    config = golden_config()
    ticks = run_ticks(config)
    initial = config.volumes.initial_products
    first = ticks[:initial]
    assert [tick.ts_us for tick in first] == [config.start_us + i for i in range(initial)]
    assert all(tick.ops[0].table is Table.PRODUCTS for tick in first)
    assert ticks[initial].ops[0].table is Table.CUSTOMERS


def test_launches_follow_the_initial_catalogue() -> None:
    config = golden_config()
    inserts = product_inserts(run_ticks(config))
    assert len(inserts) > config.volumes.initial_products
    assert all(ts > config.start_us + config.volumes.initial_products for ts, _ in inserts[40:])


def test_every_products_row_has_exactly_the_seven_columns() -> None:
    for _, op in golden_product_inserts():
        assert set(row_of(op)) == PRODUCT_COLUMNS


def test_list_price_is_a_scale_two_decimal_inside_the_configured_range() -> None:
    config = golden_config()
    low, high = config.prices.list_price_min_cents, config.prices.list_price_max_cents
    for _, op in golden_product_inserts():
        price = row_of(op)["list_price"]
        assert isinstance(price, Decimal)
        assert price.as_tuple().exponent == -2
        assert Decimal(low) / 100 <= price <= Decimal(high) / 100


def test_product_ids_run_one_two_three_with_no_gap() -> None:
    ids = [op.key["product_id"] for _, op in golden_product_inserts()]
    assert ids == list(range(1, len(ids) + 1))


def test_a_name_is_an_adjective_and_a_noun_and_a_category_is_a_list_entry() -> None:
    adjectives = set(textgen.words("product_adjectives"))
    nouns = set(textgen.words("product_nouns"))
    categories = set(textgen.words("categories"))
    for _, op in golden_product_inserts():
        row = row_of(op)
        adjective, noun = str(row["name"]).split(" ")
        assert adjective in adjectives
        assert noun in nouns
        assert row["category"] in categories


def test_a_product_insert_has_equal_created_and_updated_times_and_no_deletion() -> None:
    for ts, op in golden_product_inserts():
        row = row_of(op)
        assert row["created_at"] == clock.to_datetime(ts)
        assert row["updated_at"] == row["created_at"]
        assert row["deleted_at"] is None


def test_the_first_product_follows_the_draw_table() -> None:
    config = golden_config()
    products = Stream(config.seed, StreamName.PRODUCTS)
    text = Stream(config.seed, StreamName.TEXT)
    # `Engine.new` draws the first launch's gap before any tick runs.
    products.between(1, 2 * (clock.US_PER_DAY // config.volumes.products_per_day))
    category = products.pick(len(textgen.words("categories")))
    cents = products.between(config.prices.list_price_min_cents, config.prices.list_price_max_cents)
    adjective = text.pick(len(textgen.words("product_adjectives")))
    noun = text.pick(len(textgen.words("product_nouns")))
    assert category is not None
    assert adjective is not None
    assert noun is not None
    row = row_of(product_inserts(run_ticks(config))[0][1])
    assert row["category"] == textgen.category(category)
    assert row["name"] == textgen.product_name(adjective, noun)
    assert row["list_price"] == Decimal(cents) / 100


def test_with_no_products_the_stream_is_empty_and_its_word_counter_stays_zero() -> None:
    base = golden_config()
    volumes = dataclasses.replace(base.volumes, initial_products=0, products_per_day=0)
    config = dataclasses.replace(base, volumes=volumes)
    engine = Engine.new(config)
    ticks = list(engine.run_until(config.end_us))
    assert product_inserts(ticks) == []
    assert engine.state.streams[StreamName.PRODUCTS].words == 0
    digest = golden.build_manifest(config).digest("products")
    assert (digest.count, digest.sha256) == (0, EMPTY_SHA256)
