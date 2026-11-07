"""P12: a shorter range is a prefix, chunked live equals backfill, kill and resume equals one run.

The two-subprocess golden clause of P12 is test_determinism.py. These properties run over
Hypothesis tiny configs, which draw every volume, so each later entity is covered the moment its
process exists. "Backfill equals live" is proven with a fake pacer: arbitrary increasing `until`
values; Phase 4's real pacer calls the same `run_until`.
"""

from __future__ import annotations

import dataclasses

from hypothesis import given, settings
from hypothesis import strategies as st
from shopstream_generator import clock
from shopstream_generator.config import ModelConfig
from shopstream_generator.engine import Engine
from shopstream_generator.ops import STREAMS

from . import harness
from .strategies import CANARY, START, tiny_configs


def offset(config: ModelConfig, data: st.DataObject, label: str) -> int:
    """A drawn instant in [start, end]."""
    return config.start_us + data.draw(st.integers(0, config.end_us - config.start_us), label=label)


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_a_shorter_range_is_a_line_prefix(config: ModelConfig, data: st.DataObject) -> None:
    first, second = sorted((offset(config, data, "t1"), offset(config, data, "t2")))
    short = harness.lines_by_stream(harness.run_ticks(config, until=first))
    longer = harness.lines_by_stream(harness.run_ticks(config, until=second))
    for name in STREAMS:
        assert longer[name][: len(short[name])] == short[name], name


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_chunked_live_equals_backfill(config: ModelConfig, data: st.DataObject) -> None:
    untils = sorted(
        data.draw(st.lists(st.integers(config.start_us, config.end_us), max_size=8), label="untils")
    )
    chunked = harness.run_chunked(config, untils)
    backfill = harness.run_ticks(config)
    assert harness.signature(chunked) == harness.signature(backfill)


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_kill_and_resume_equals_uninterrupted(config: ModelConfig, data: st.DataObject) -> None:
    kill_at = offset(config, data, "kill_at")
    resumed, resumed_state = harness.run_resumed(config, kill_at)
    whole, whole_state = harness.run_with_state(config)
    assert harness.signature(resumed) == harness.signature(whole)
    assert resumed_state == whole_state


def fixed_config() -> ModelConfig:
    base = ModelConfig.default(canary_token=CANARY)
    volumes = dataclasses.replace(
        base.volumes, initial_customers=5, customers_per_day=40, products_per_day=0
    )
    end_us = START + 2 * clock.US_PER_DAY
    return dataclasses.replace(base, seed=11, start_us=START, end_us=end_us, volumes=volumes)


def test_a_chunk_boundary_on_a_tick_defers_it() -> None:
    config = fixed_config()
    whole = harness.run_ticks(config)
    boundary = whole[2].ts_us
    engine = Engine.new(config)
    before = list(engine.run_until(boundary))
    assert [tick.seq for tick in before] == [1, 2]
    after = list(engine.run_until(config.end_us))
    assert after[0] == whole[2]
    assert harness.signature(before + after) == harness.signature(whole)
    assert harness.signature(harness.run_chunked(config, [boundary])) == harness.signature(whole)


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_an_empty_chunk_changes_nothing(config: ModelConfig, data: st.DataObject) -> None:
    until = offset(config, data, "until")
    engine = Engine.new(config)
    list(engine.run_until(until))
    before = engine.state.to_json()
    assert list(engine.run_until(until)) == []
    assert engine.state.to_json() == before
