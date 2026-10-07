"""The engine's whole state survives JSON text, and a resume refuses another model's state (D-24).

The world section holds only ints and None (D-18: no text), so a serialized state carries no
personal-looking data. Field coverage is generic: the world's keys and record widths are read from
the `World` dataclass, so a field a later plan adds and forgets to serialize fails here.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any, get_args, get_type_hints

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from shopstream_generator import clock
from shopstream_generator.config import ModelConfig
from shopstream_generator.engine import Engine
from shopstream_generator.rng import StreamName
from shopstream_generator.state import EngineState, StateError, World

from .strategies import CANARY, tiny_configs


def killed_at(config: ModelConfig, data: st.DataObject) -> Engine:
    """An engine run to a drawn instant in [start, end]."""
    until = config.start_us + data.draw(st.integers(0, config.end_us - config.start_us))
    engine = Engine.new(config)
    list(engine.run_until(until))
    return engine


def one_day_config() -> ModelConfig:
    """The backfill defaults over one simulated day."""
    config = ModelConfig.default(canary_token=CANARY)
    return dataclasses.replace(config, end_us=config.start_us + clock.US_PER_DAY)


def through_text(state: EngineState) -> Any:
    parsed: Any = json.loads(json.dumps(state.to_json()))
    return parsed


def leaves(obj: object) -> list[object]:
    if isinstance(obj, dict):
        return [leaf for value in obj.values() for leaf in leaves(value)]
    if isinstance(obj, list):
        return [leaf for value in obj for leaf in leaves(value)]
    return [obj]


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_state_round_trips_through_json_text(config: ModelConfig, data: st.DataObject) -> None:
    state = killed_at(config, data).state
    parsed = through_text(state)
    assert parsed["cdc"] == sorted(parsed["cdc"])
    restored = EngineState.from_json(parsed, config)
    assert restored.to_json() == state.to_json()
    for name in StreamName:
        assert restored.streams[name].words == state.streams[name].words
        assert restored.streams[name].dump() == state.streams[name].dump()


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_the_world_section_holds_only_ints_and_none(
    config: ModelConfig, data: st.DataObject
) -> None:
    world = killed_at(config, data).state.to_json()["world"]
    assert all(leaf is None or type(leaf) is int for leaf in leaves(world))


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_every_world_field_and_record_field_is_serialized(
    config: ModelConfig, data: st.DataObject
) -> None:
    world = killed_at(config, data).state.to_json()["world"]
    assert isinstance(world, dict)
    hints = get_type_hints(World)
    assert list(world) == [f.name for f in dataclasses.fields(World)]
    for name, rows in world.items():
        record_type = get_args(hints[name])[1]
        width = len(dataclasses.fields(record_type))
        assert all(len(row) == width for row in rows), name


def test_a_late_state_holds_customers() -> None:
    config = one_day_config()
    engine = Engine.new(config)
    list(engine.run_until(config.end_us))
    world = engine.state.to_json()["world"]
    assert isinstance(world, dict)
    assert len(world["customers"]) > 100


def test_every_serialized_section_is_present() -> None:
    config = one_day_config()
    parsed = through_text(Engine.new(config).state)
    assert list(parsed) == ["format", "meta", "rng", "cdc", "events", "world"]
    assert parsed["format"] == 1
    assert list(parsed["rng"]) == [name.value for name in StreamName]
    assert list(parsed["meta"]) == [
        "model_sha256",
        "last_ts_us",
        "tick_seq",
        "heap_seq",
        "next_ids",
    ]


@settings(max_examples=30)
@given(config=tiny_configs(), data=st.data())
def test_resume_refuses_another_seed_and_accepts_another_range_end_and_speed(
    config: ModelConfig, data: st.DataObject
) -> None:
    parsed = through_text(killed_at(config, data).state)
    with pytest.raises(StateError, match="model_sha256"):
        Engine.resume(dataclasses.replace(config, seed=config.seed + 1), parsed)
    longer = dataclasses.replace(
        config, end_us=config.end_us + clock.US_PER_DAY, speed=config.speed + 1
    )
    assert Engine.resume(longer, parsed).state.to_json() == parsed


def malformed(change: Any) -> dict[str, Any]:
    config = one_day_config()
    engine = Engine.new(config)
    list(engine.run_until(config.start_us + clock.US_PER_HOUR))
    parsed = through_text(engine.state)
    assert parsed["cdc"], "the case needs a pending item"
    assert parsed["world"]["customers"], "the case needs a customer"
    change(parsed)
    return dict(parsed)


def drop_section(parsed: dict[str, Any]) -> None:
    del parsed["events"]


def extra_key(parsed: dict[str, Any]) -> None:
    parsed["extra"] = 1


def old_format(parsed: dict[str, Any]) -> None:
    parsed["format"] = 2


def bool_in_heap(parsed: dict[str, Any]) -> None:
    parsed["cdc"][0][3] = True


def float_in_heap(parsed: dict[str, Any]) -> None:
    parsed["cdc"][0][0] = 1.5


def short_heap_item(parsed: dict[str, Any]) -> None:
    parsed["cdc"][0] = parsed["cdc"][0][:4]


def none_in_a_required_field(parsed: dict[str, Any]) -> None:
    parsed["world"]["customers"][0][0] = None


def text_in_the_world(parsed: dict[str, Any]) -> None:
    parsed["world"]["customers"][0][1] = "a@example.test"


def short_world_row(parsed: dict[str, Any]) -> None:
    parsed["world"]["customers"][0] = parsed["world"]["customers"][0][:2]


def duplicate_world_id(parsed: dict[str, Any]) -> None:
    parsed["world"]["customers"].append(copy.deepcopy(parsed["world"]["customers"][0]))


def unknown_world_table(parsed: dict[str, Any]) -> None:
    parsed["world"]["ghosts"] = []


def short_mt(parsed: dict[str, Any]) -> None:
    parsed["rng"]["customers"]["mt"] = parsed["rng"]["customers"]["mt"][:10]


def missing_stream(parsed: dict[str, Any]) -> None:
    del parsed["rng"]["text"]


def float_counter(parsed: dict[str, Any]) -> None:
    parsed["meta"]["tick_seq"] = 2.0


@pytest.mark.parametrize(
    "change",
    [
        drop_section,
        extra_key,
        old_format,
        bool_in_heap,
        float_in_heap,
        short_heap_item,
        none_in_a_required_field,
        text_in_the_world,
        short_world_row,
        duplicate_world_id,
        unknown_world_table,
        short_mt,
        missing_stream,
        float_counter,
    ],
)
def test_a_malformed_state_raises_state_error_and_never_echoes_a_value(change: Any) -> None:
    config = one_day_config()
    parsed = malformed(change)
    with pytest.raises(StateError) as caught:
        Engine.resume(config, parsed)
    assert "example.test" not in str(caught.value)


def test_a_state_that_is_not_an_object_raises() -> None:
    config = one_day_config()
    with pytest.raises(StateError):
        Engine.resume(config, [])
