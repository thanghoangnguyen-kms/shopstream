"""The catalogue: customers (and, from Task 2, products) carry their full REF section 7 columns.

Every case runs the golden config through a real `Engine`, so a row here is the row a sink will
see. Names, cities and emails come only from the committed word lists and the country table
(D-18), and the email uses the reserved example.com domain (RFC 2606, T-02-12).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from shopstream_generator import clock, countries, textgen
from shopstream_generator import config as model_config
from shopstream_generator.config import ModelConfig
from shopstream_generator.engine import Engine
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


def golden() -> ModelConfig:
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
    config = golden()
    inserts = customer_inserts(run_ticks(config))
    initial = config.volumes.initial_customers
    stamps = [ts for ts, _ in inserts[:initial]]
    expected = [config.start_us + config.volumes.initial_products + j for j in range(initial)]
    assert stamps == expected


def test_a_sign_up_follows_the_initial_base_with_an_integer_gap() -> None:
    config = golden()
    inserts = customer_inserts(run_ticks(config))
    assert len(inserts) > config.volumes.initial_customers
    first_arrival = inserts[config.volumes.initial_customers][0]
    assert (
        first_arrival
        > config.start_us + config.volumes.initial_products + config.volumes.initial_customers
    )


def test_every_customers_row_has_exactly_the_eight_columns() -> None:
    for _, op in customer_inserts(run_ticks(golden())):
        assert set(row_of(op)) == CUSTOMER_COLUMNS


def test_every_email_uses_example_com_with_the_rows_own_customer_id() -> None:
    for _, op in customer_inserts(run_ticks(golden())):
        row = row_of(op)
        match = EMAIL.fullmatch(str(row["email"]))
        assert match is not None
        assert int(match.group(1)) == row["customer_id"]


def test_customer_ids_run_one_two_three_with_no_gap() -> None:
    ids = [op.key["customer_id"] for _, op in customer_inserts(run_ticks(golden()))]
    assert ids == list(range(1, len(ids) + 1))


def test_a_rows_country_and_city_come_from_the_same_table_row() -> None:
    pairs = {(c.code, city) for c in countries.countries() for city in c.cities}
    for _, op in customer_inserts(run_ticks(golden())):
        row = row_of(op)
        assert (row["country"], row["city"]) in pairs


def test_a_full_name_is_a_first_and_a_last_name_from_the_lists() -> None:
    firsts, lasts = set(textgen.words("first_names")), set(textgen.words("last_names"))
    for _, op in customer_inserts(run_ticks(golden())):
        first, last = str(row_of(op)["full_name"]).split(" ")
        assert first in firsts
        assert last in lasts


def test_an_insert_row_has_equal_created_and_updated_times_and_no_deletion() -> None:
    for ts, op in customer_inserts(run_ticks(golden())):
        row = row_of(op)
        moment = clock.to_datetime(ts)
        assert row["created_at"] == moment
        assert row["updated_at"] == moment
        assert row["deleted_at"] is None
        assert isinstance(row["created_at"], datetime)


def test_the_first_customer_follows_the_draw_table() -> None:
    config = golden()
    customers = Stream(config.seed, StreamName.CUSTOMERS)
    text = Stream(config.seed, StreamName.TEXT)
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
    config = golden()
    engine = Engine.new(config)
    list(engine.run_until(config.end_us))
    records = engine.state.world.customers
    assert len(records) > config.volumes.initial_customers
    for record in records.values():
        for value in vars(record).values():
            assert value is None or type(value) is int
