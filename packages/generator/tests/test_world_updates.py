"""In-life updates (BIZ-01, BIZ-02): moves, edits, soft deletes and discontinuations (D-03, D-09, D-17).

One update process draws a fixed set of numbers on every arrival whatever kind fires and whether
a target exists, so two configs with equal weight totals but different kinds place their update
ticks at the same instants. Tests here run real engines, plus direct `apply_update` calls on
hand-built state to pin exactly which columns each kind changes.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from hypothesis import given, settings
from hypothesis import strategies as st
from shopstream_generator import clock, countries, textgen
from shopstream_generator.config import ModelConfig, UpdateKind
from shopstream_generator.engine import Engine
from shopstream_generator.ops import Op, OpKind, Table, Tick
from shopstream_generator.rng import Stream, StreamName
from shopstream_generator.state import CustomerRec, EngineState, ProductRec
from shopstream_generator.world import customers, products, updates

from . import harness
from .strategies import CANARY, START, tiny_configs

CUSTOMER_KINDS = (1, 1, 1, 1, 0, 0, 0, 0)
PRODUCT_KINDS = (0, 0, 0, 0, 1, 1, 1, 1)
TS = START + 5_000_000

CUSTOMER_GROUPS = {
    "move": {"city", "country"},
    "email": {"email"},
    "name": {"full_name"},
    "delete": {"deleted_at"},
}
PRODUCT_GROUPS = {
    "name": {"name"},
    "category": {"category"},
    "price": {"list_price"},
    "discontinue": {"deleted_at"},
}


def update_config(
    *,
    weights: tuple[int, ...] = (3, 3, 3, 1, 2, 2, 2, 1),
    initial_customers: int = 300,
    initial_products: int = 300,
    customers_per_day: int = 20,
    products_per_day: int = 5,
    updates_per_day: int = 200,
    days: int = 2,
    seed: int = 5,
) -> ModelConfig:
    """A config with no orders, so only inserts and updates make ticks."""
    base = ModelConfig.default(canary_token=CANARY)
    volumes = dataclasses.replace(
        base.volumes,
        initial_customers=initial_customers,
        initial_products=initial_products,
        customers_per_day=customers_per_day,
        products_per_day=products_per_day,
        orders_per_day=0,
        updates_per_day=updates_per_day,
    )
    return dataclasses.replace(
        base,
        seed=seed,
        start_us=START,
        end_us=START + days * clock.US_PER_DAY,
        volumes=volumes,
        update_kind_weights=weights,
    )


def update_ops(ticks: list[Tick]) -> list[tuple[Tick, Op]]:
    return [(tick, op) for tick in ticks for op in tick.ops if op.kind is OpKind.UPDATE]


def key_of(op: Op) -> tuple[str, int]:
    (value,) = op.key.values()
    return op.table.value, value


def need(value: int | None) -> int:
    assert value is not None
    return value


def row_of(op: Op) -> Mapping[str, object]:
    assert op.row is not None
    return op.row


# ---------------------------------------------------------------- one update per tick, by column


def test_an_update_tick_holds_one_full_after_image_that_changes_only_its_columns() -> None:
    ticks = harness.run_ticks(update_config())
    previous: dict[tuple[str, int], Mapping[str, object]] = {}
    seen: set[str] = set()
    updates_seen = 0
    for tick in ticks:
        for op in tick.ops:
            if op.kind is OpKind.INSERT:
                previous[key_of(op)] = row_of(op)
                continue
            assert op.kind is OpKind.UPDATE
            assert len(tick.ops) == 1, "an update tick holds exactly one op"
            updates_seen += 1
            row = row_of(op)
            before = previous[key_of(op)]
            assert set(row) == set(before), "an update is the full after-image"
            assert row["updated_at"] == clock.to_datetime(tick.ts_us)
            changed = {column for column in row if row[column] != before[column]} - {"updated_at"}
            groups = CUSTOMER_GROUPS if op.table is Table.CUSTOMERS else PRODUCT_GROUPS
            if changed:
                matching = [name for name, columns in groups.items() if changed <= columns]
                assert len(matching) == 1, f"changed columns {sorted(changed)} fit no single kind"
                seen.add(f"{op.table.value}.{matching[0]}")
            if changed == {"deleted_at"}:
                assert before["deleted_at"] is None
                assert row["deleted_at"] == clock.to_datetime(tick.ts_us)
            assert row["created_at"] == before["created_at"]
            previous[key_of(op)] = row
    assert updates_seen > 100
    assert seen == {f"customers.{name}" for name in CUSTOMER_GROUPS} | {
        f"products.{name}" for name in PRODUCT_GROUPS
    }


def test_a_soft_deleted_customer_and_a_discontinued_product_are_never_touched_again() -> None:
    ticks = harness.run_ticks(update_config())
    deleted_at: dict[tuple[str, int], int] = {}
    for tick in ticks:
        for op in tick.ops:
            key = key_of(op)
            assert key not in deleted_at, "an op touched a key after its soft delete"
            if op.kind is OpKind.UPDATE and row_of(op)["deleted_at"] is not None:
                deleted_at[key] = tick.ts_us
    kinds = {table for table, _ in deleted_at}
    assert kinds == {"customers", "products"}, "the run holds both kinds of final update"


def test_each_key_has_at_most_one_update_that_sets_deleted_at() -> None:
    ticks = harness.run_ticks(update_config())
    deletions = [key_of(op) for _, op in update_ops(ticks) if row_of(op)["deleted_at"] is not None]
    assert deletions
    assert len(deletions) == len(set(deletions))


# ---------------------------------------------------------------- the draw table


def test_the_draws_follow_the_table_in_order_on_the_lifecycle_and_text_streams() -> None:
    config = update_config()
    live_customers, live_products = 17, 9
    by_hand = {name: Stream(config.seed, name) for name in StreamName}
    lifecycle, text = by_hand[StreamName.LIFECYCLE], by_hand[StreamName.TEXT]
    kind = UpdateKind(lifecycle.choice_index(config.update_kind_weights))
    customer_pick = lifecycle.pick(live_customers)
    product_pick = lifecycle.pick(live_products)
    country_idx = lifecycle.choice_index(countries.country_weights())
    city_pick = need(lifecycle.pick(len(countries.countries()[country_idx].cities)))
    price_cents = lifecycle.between(
        config.prices.list_price_min_cents, config.prices.list_price_max_cents
    )
    category_pick = need(lifecycle.pick(len(textgen.words("categories"))))
    first_idx = need(text.pick(len(textgen.words("first_names"))))
    last_idx = need(text.pick(len(textgen.words("last_names"))))
    adjective_idx = need(text.pick(len(textgen.words("product_adjectives"))))
    noun_idx = need(text.pick(len(textgen.words("product_nouns"))))

    streams = {name: Stream(config.seed, name) for name in StreamName}
    drawn = updates.draw_update(streams, config, live_customers, live_products)
    assert drawn == updates.UpdateDraws(
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
    for name in StreamName:
        assert streams[name].words == by_hand[name].words, name


def test_an_empty_population_draws_the_same_words_and_returns_no_pick() -> None:
    config = update_config()
    full = {name: Stream(config.seed, name) for name in StreamName}
    empty = {name: Stream(config.seed, name) for name in StreamName}
    updates.draw_update(full, config, 5, 5)
    drawn = updates.draw_update(empty, config, 0, 0)
    assert drawn.customer_pick is None
    assert drawn.product_pick is None
    for name in StreamName:
        assert empty[name].words == full[name].words, name


# ---------------------------------------------------------------- D-09: draws never depend on the kind


def test_two_configs_with_equal_weight_totals_place_update_ticks_at_the_same_instants() -> None:
    customer_run = harness.run_ticks(update_config(weights=CUSTOMER_KINDS, days=1))
    product_run = harness.run_ticks(update_config(weights=PRODUCT_KINDS, days=1))
    customer_updates = update_ops(customer_run)
    product_updates = update_ops(product_run)
    assert len(customer_updates) > 100
    assert {op.table for _, op in customer_updates} == {Table.CUSTOMERS}
    assert {op.table for _, op in product_updates} == {Table.PRODUCTS}
    assert [tick.ts_us for tick, _ in customer_updates] == [
        tick.ts_us for tick, _ in product_updates
    ]


def test_an_arrival_with_no_target_is_a_no_op_that_still_draws() -> None:
    config = update_config(weights=CUSTOMER_KINDS, initial_customers=0, customers_per_day=0, days=1)
    engine = Engine.new(config)
    ticks = list(engine.run_until(config.end_us))
    assert update_ops(ticks) == []
    assert engine.state.next_ids["customer"] == 1
    assert engine.state.streams[StreamName.LIFECYCLE].words > 0
    assert engine.state.streams[StreamName.TEXT].words > 0


def test_a_zero_update_rate_schedules_nothing_and_draws_nothing() -> None:
    config = update_config(updates_per_day=0, days=1)
    engine = Engine.new(config)
    assert [item for item in engine.state.cdc if item[2] == 5] == []
    list(engine.run_until(config.end_us))
    assert engine.state.streams[StreamName.LIFECYCLE].words == 0


def test_the_first_update_arrival_is_one_lifecycle_gap_after_the_start() -> None:
    config = update_config(days=1)
    mean = clock.US_PER_DAY // config.volumes.updates_per_day
    gap = Stream(config.seed, StreamName.LIFECYCLE).between(1, 2 * mean)
    engine = Engine.new(config)
    arrivals = [item for item in engine.state.cdc if item[2] == 5]
    assert [item[0] for item in arrivals] == [config.start_us + gap]


def test_an_update_arrival_reschedules_from_its_due_time() -> None:
    config = update_config(days=1)
    mean = clock.US_PER_DAY // config.volumes.updates_per_day
    lifecycle = Stream(config.seed, StreamName.LIFECYCLE)
    due = config.start_us + lifecycle.between(1, 2 * mean)
    engine = Engine.new(config)
    # Run exactly to the first update's tick; the heap then holds the second, rescheduled
    # from the first one's due time (not from its tick).
    ticks = list(engine.run_until(due + 1))
    assert [tick.ts_us for tick, _ in update_ops(ticks)] == [due]
    second_gap = lifecycle.between(1, 2 * mean)
    arrivals = [item for item in engine.state.cdc if item[2] == 5]
    assert [item[0] for item in arrivals] == [due + second_gap]


# ---------------------------------------------------------------- apply_update on hand-built state


def customer_rec(customer_id: int, **overrides: int | None) -> CustomerRec:
    fields: dict[str, int | None] = {
        "customer_id": customer_id,
        "created_us": START + customer_id,
        "deleted_us": None,
        "first_idx": 0,
        "last_idx": 0,
        "email_first_idx": 0,
        "email_last_idx": 0,
        "email_version": 0,
        "country_idx": 0,
        "city_idx": 0,
        **overrides,
    }
    return CustomerRec(**fields)  # type: ignore[arg-type]


def product_rec(product_id: int) -> ProductRec:
    return ProductRec(
        product_id=product_id,
        created_us=START + product_id,
        deleted_us=None,
        adjective_idx=0,
        noun_idx=0,
        category_idx=0,
        list_price_cents=1000,
    )


def draws_for(kind: UpdateKind, **overrides: int | None) -> updates.UpdateDraws:
    fields: dict[str, object] = {
        "kind": kind,
        "customer_pick": 1,
        "product_pick": 1,
        "country_idx": 0,
        "city_pick": 0,
        "price_cents": 1000,
        "category_pick": 0,
        "first_idx": 0,
        "last_idx": 0,
        "adjective_idx": 0,
        "noun_idx": 0,
        **overrides,
    }
    return updates.UpdateDraws(**fields)  # type: ignore[arg-type]


def small_world(n: int = 3) -> tuple[EngineState, list[int], list[int]]:
    state = EngineState.new(update_config())
    for i in range(1, n + 1):
        state.world.customers[i] = customer_rec(i)
        state.world.products[i] = product_rec(i)
    live = list(range(1, n + 1))
    return state, live, list(live)


def changed_columns(before: Mapping[str, object], after: Mapping[str, object]) -> set[str]:
    return {column for column in after if after[column] != before[column]}


def test_a_move_changes_city_and_country_together_and_nothing_else() -> None:
    state, live_c, live_p = small_world()
    before = customers.row(state.world.customers[2], TS)
    cities = countries.countries()[1].cities
    op = updates.apply_update(
        state,
        live_c,
        live_p,
        TS,
        draws_for(UpdateKind.CUSTOMER_MOVE, country_idx=1, city_pick=len(cities) - 1),
    )
    assert op is not None
    assert (op.table, op.kind, dict(op.key)) == (Table.CUSTOMERS, OpKind.UPDATE, {"customer_id": 2})
    after = row_of(op)
    assert changed_columns(before, after) == {"city", "country", "updated_at"}
    assert after["country"] == countries.countries()[1].code
    assert after["city"] == cities[-1]
    assert live_c == [1, 2, 3]


def test_an_email_edit_changes_only_the_email_and_bumps_its_version() -> None:
    state, live_c, live_p = small_world()
    state.world.customers[2] = customer_rec(2, first_idx=3, last_idx=4)
    before = customers.row(state.world.customers[2], TS)
    op = updates.apply_update(state, live_c, live_p, TS, draws_for(UpdateKind.CUSTOMER_EMAIL))
    assert op is not None
    assert changed_columns(before, row_of(op)) == {"email", "updated_at"}
    rec = state.world.customers[2]
    assert (rec.email_first_idx, rec.email_last_idx, rec.email_version) == (3, 4, 1)


def test_a_name_edit_changes_only_the_full_name() -> None:
    state, live_c, live_p = small_world()
    before = customers.row(state.world.customers[2], TS)
    op = updates.apply_update(
        state, live_c, live_p, TS, draws_for(UpdateKind.CUSTOMER_NAME, first_idx=5, last_idx=6)
    )
    assert op is not None
    assert changed_columns(before, row_of(op)) == {"full_name", "updated_at"}
    assert row_of(op)["email"] == before["email"]


def test_a_soft_delete_sets_deleted_at_to_the_tick_and_leaves_the_live_list() -> None:
    state, live_c, live_p = small_world()
    before = customers.row(state.world.customers[2], TS)
    op = updates.apply_update(state, live_c, live_p, TS, draws_for(UpdateKind.CUSTOMER_SOFT_DELETE))
    assert op is not None
    assert changed_columns(before, row_of(op)) == {"deleted_at", "updated_at"}
    assert row_of(op)["deleted_at"] == clock.to_datetime(TS)
    assert state.world.customers[2].deleted_us == TS
    assert live_c == [1, 3]
    assert live_p == [1, 2, 3]


def test_product_edits_change_one_column_each() -> None:
    expected = {
        UpdateKind.PRODUCT_NAME: ("name", {"adjective_idx": 4, "noun_idx": 5}),
        UpdateKind.PRODUCT_CATEGORY: ("category", {"category_pick": 2}),
        UpdateKind.PRODUCT_PRICE: ("list_price", {"price_cents": 2345}),
    }
    for kind, (column, overrides) in expected.items():
        state, live_c, live_p = small_world()
        before = products.row(state.world.products[2], TS)
        op = updates.apply_update(state, live_c, live_p, TS, draws_for(kind, **overrides))
        assert op is not None, kind
        assert (op.table, dict(op.key)) == (Table.PRODUCTS, {"product_id": 2}), kind
        assert changed_columns(before, row_of(op)) == {column, "updated_at"}, kind
        assert live_p == [1, 2, 3], kind


def test_a_discontinuation_sets_deleted_at_and_leaves_the_live_list() -> None:
    state, live_c, live_p = small_world()
    op = updates.apply_update(state, live_c, live_p, TS, draws_for(UpdateKind.PRODUCT_DISCONTINUE))
    assert op is not None
    assert row_of(op)["deleted_at"] == clock.to_datetime(TS)
    assert state.world.products[2].deleted_us == TS
    assert live_p == [1, 3]
    assert live_c == [1, 2, 3]


def test_a_missing_target_is_a_no_op_that_changes_nothing() -> None:
    state, live_c, live_p = small_world()
    snapshot = state.to_json()
    assert (
        updates.apply_update(
            state, live_c, live_p, TS, draws_for(UpdateKind.CUSTOMER_NAME, customer_pick=None)
        )
        is None
    )
    assert (
        updates.apply_update(
            state, live_c, live_p, TS, draws_for(UpdateKind.PRODUCT_PRICE, product_pick=None)
        )
        is None
    )
    assert state.to_json() == snapshot
    assert live_c == [1, 2, 3]
    assert live_p == [1, 2, 3]


def test_the_target_is_an_index_into_the_ascending_live_list() -> None:
    state, live_c, live_p = small_world(5)
    live_c.remove(2)
    op = updates.apply_update(
        state, live_c, live_p, TS, draws_for(UpdateKind.CUSTOMER_NAME, customer_pick=1, first_idx=7)
    )
    assert op is not None
    assert dict(op.key) == {"customer_id": 3}


# ---------------------------------------------------------------- P12 over update-heavy worlds


def soft_delete_counts(ticks: list[Tick]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for _, op in update_ops(ticks):
        if op.table is Table.CUSTOMERS and row_of(op)["deleted_at"] is not None:
            (customer_id,) = op.key.values()
            counts[customer_id] = counts.get(customer_id, 0) + 1
    return counts


def deletion_config(config: ModelConfig) -> ModelConfig:
    volumes = dataclasses.replace(config.volumes, updates_per_day=200)
    return dataclasses.replace(
        config, volumes=volumes, update_kind_weights=(0, 0, 0, 1, 0, 0, 0, 0)
    )


@settings(max_examples=25)
@given(config=tiny_configs(), data=st.data())
def test_a_resume_across_soft_deletes_yields_one_soft_delete_per_customer(
    config: ModelConfig, data: st.DataObject
) -> None:
    config = deletion_config(config)
    kill_at = config.start_us + data.draw(
        st.integers(0, config.end_us - config.start_us), label="kill_at"
    )
    resumed, _ = harness.run_resumed(config, kill_at)
    whole = harness.run_ticks(config)
    counts = soft_delete_counts(resumed)
    assert counts == soft_delete_counts(whole)
    assert all(count == 1 for count in counts.values())
    assert harness.signature(resumed) == harness.signature(whole)


def test_a_resume_in_the_middle_of_a_run_with_soft_deletes_is_not_vacuous() -> None:
    config = update_config(weights=(0, 0, 0, 1, 0, 0, 0, 0), initial_customers=50, days=1)
    whole = harness.run_ticks(config)
    counts = soft_delete_counts(whole)
    assert len(counts) > 20
    for fraction in (1, 2, 3):
        kill_at = config.start_us + fraction * clock.US_PER_DAY // 4
        resumed, _ = harness.run_resumed(config, kill_at)
        assert soft_delete_counts(resumed) == counts


def test_a_resume_across_a_discontinuation_yields_the_same_products_stream() -> None:
    config = update_config(weights=(0, 0, 0, 0, 1, 1, 1, 8), initial_products=60, days=1)
    whole = harness.run_ticks(config)
    discontinuations = [
        tick.ts_us
        for tick, op in update_ops(whole)
        if op.table is Table.PRODUCTS and row_of(op)["deleted_at"] is not None
    ]
    assert len(discontinuations) > 5
    expected = harness.lines_by_stream(whole)["products"]
    for kill_at in (discontinuations[2], discontinuations[2] + 1):
        resumed, _ = harness.run_resumed(config, kill_at)
        assert harness.lines_by_stream(resumed)["products"] == expected
