"""The engine's tick rule and its hazards: ties, empty ranges, the stop before `until` (CORE-02)."""

from __future__ import annotations

import dataclasses
from itertools import pairwise

import pytest
from shopstream_generator import clock
from shopstream_generator.config import ModelConfig
from shopstream_generator.engine import Engine, ItemKind
from shopstream_generator.ops import Tick
from shopstream_generator.rng import Stream, StreamName

from .strategies import CANARY

START = clock.parse("2025-06-28T00:00:00.000000Z")
END = clock.parse("2025-07-05T00:00:00.000000Z")


def config(initial: int = 0, per_day: int = 0, seed: int = 3) -> ModelConfig:
    """Only customers run; every other process is off, so these tests keep their meaning."""
    base = ModelConfig.default(canary_token=CANARY)
    volumes = dataclasses.replace(
        base.volumes,
        initial_customers=initial,
        initial_products=0,
        customers_per_day=per_day,
        products_per_day=0,
        orders_per_day=0,
        updates_per_day=0,
    )
    return dataclasses.replace(base, seed=seed, start_us=START, end_us=END, volumes=volumes)


def customer_ids(ticks: list[Tick]) -> list[int]:
    return [tick.ops[0].key["customer_id"] for tick in ticks]


def test_two_items_due_at_the_same_microsecond_become_two_ticks_in_push_order() -> None:
    engine = Engine.new(config())
    engine.push(START + 5, ItemKind.INITIAL_CUSTOMER)
    engine.push(START + 5, ItemKind.INITIAL_CUSTOMER)
    ticks = list(engine.run_until(END))
    assert [tick.ts_us for tick in ticks] == [START + 5, START + 6]
    assert [tick.seq for tick in ticks] == [1, 2]
    assert customer_ids(ticks) == [1, 2]


def test_a_tie_chain_keeps_shifting_by_one_microsecond() -> None:
    engine = Engine.new(config())
    for _ in range(4):
        engine.push(START + 10, ItemKind.INITIAL_CUSTOMER)
    ticks = list(engine.run_until(END))
    assert [tick.ts_us for tick in ticks] == [START + 10, START + 11, START + 12, START + 13]


def test_ticks_rise_strictly_and_the_first_is_at_or_after_the_start() -> None:
    ticks = list(Engine.new(config(initial=20, per_day=50)).run_until(END))
    stamps = [tick.ts_us for tick in ticks]
    assert stamps[0] >= START
    assert all(later > earlier for earlier, later in pairwise(stamps))
    assert [tick.seq for tick in ticks] == list(range(1, len(ticks) + 1))
    assert all(tick.ops for tick in ticks)


def test_a_range_with_no_due_item_yields_no_tick() -> None:
    assert list(Engine.new(config()).run_until(END)) == []
    # A sign-up rate is set, but its first arrival is at least one microsecond after the start.
    assert list(Engine.new(config(per_day=10)).run_until(START)) == []


def test_run_until_stops_before_the_first_item_at_or_after_until_and_keeps_it() -> None:
    engine = Engine.new(config(initial=3))
    ticks = list(engine.run_until(START + 2))
    assert [tick.ts_us for tick in ticks] == [START, START + 1]
    assert len(engine.state.cdc) == 1
    assert engine.state.cdc[0][0] == START + 2
    assert engine.state.tick_seq == 2
    assert engine.state.last_ts_us == START + 1


def test_chunked_runs_equal_one_run() -> None:
    whole = list(Engine.new(config(initial=5, per_day=40)).run_until(END))
    engine = Engine.new(config(initial=5, per_day=40))
    chunked: list[Tick] = []
    cut = START
    while cut < END:
        cut = min(cut + 7 * clock.US_PER_HOUR + 13, END)
        chunked.extend(engine.run_until(cut))
    assert chunked == whole


def test_a_tie_shifted_item_does_not_slip_past_until() -> None:
    engine = Engine.new(config())
    engine.push(START + 5, ItemKind.INITIAL_CUSTOMER)
    engine.push(START + 5, ItemKind.INITIAL_CUSTOMER)
    ticks = list(engine.run_until(START + 6))
    assert [tick.ts_us for tick in ticks] == [START + 5]
    assert len(engine.state.cdc) == 1


def test_heap_items_are_tuples_of_ints_with_a_unique_increasing_seq() -> None:
    engine = Engine.new(config(initial=4, per_day=10))
    items = engine.state.cdc
    assert all(len(item) == 5 and all(type(x) is int for x in item) for item in items)
    seqs = [item[1] for item in items]
    assert len(set(seqs)) == len(seqs)
    assert sorted(seqs) == list(range(1, len(seqs) + 1))


def test_a_zero_rate_schedules_no_arrival_and_draws_nothing() -> None:
    engine = Engine.new(config(initial=3, per_day=0))
    assert len(engine.state.cdc) == 3
    assert list(engine.run_until(END))
    assert all(stream.words == 0 for stream in engine.state.streams.values())
    assert engine.state.cdc == []


def test_an_arrival_reschedules_from_its_due_time_not_its_tick() -> None:
    # One sign-up a day: the natural first arrival is a day or more away, far from the pair below.
    engine = Engine.new(config(per_day=1))
    replica = Stream(3, StreamName.CUSTOMERS)
    mean_gap = clock.US_PER_DAY
    natural, second, third = (replica.between(1, 2 * mean_gap) for _ in range(3))
    # Two arrivals due at the same microsecond: the second ticks 1 us later but reschedules from due.
    engine.push(START + 100, ItemKind.CUSTOMER_ARRIVAL)
    engine.push(START + 100, ItemKind.CUSTOMER_ARRIVAL)
    ticks = list(engine.run_until(START + 102))
    assert [tick.ts_us for tick in ticks] == [START + 100, START + 101]
    dues = sorted(item[0] for item in engine.state.cdc)
    assert dues == sorted([START + natural, START + 100 + second, START + 100 + third])


def test_an_unknown_item_kind_is_refused() -> None:
    engine = Engine.new(config())
    engine.state.cdc.append((START, 99, 99, 0, 0))
    with pytest.raises(ValueError, match="item kind"):
        list(engine.run_until(END))


def test_customer_rows_are_full_after_images_at_the_tick() -> None:
    ticks = list(Engine.new(config(initial=1)).run_until(END))
    row = ticks[0].ops[0].row
    assert row is not None
    assert row["customer_id"] == 1
    assert row["created_at"] == row["updated_at"] == clock.to_datetime(START)
    assert row["deleted_at"] is None
