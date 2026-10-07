"""Shared run helpers for P12 and the later property suite.

Every helper drives a real `Engine` and returns plain data. `run_resumed` serializes through JSON
*text* and reads it back, never a shared object, so it proves the text form and not just that two
references agree. The sink arrives with a later plan, which extends these helpers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from shopstream_generator import canon
from shopstream_generator.config import ModelConfig
from shopstream_generator.engine import Engine
from shopstream_generator.manifest import Manifest, StreamHasher
from shopstream_generator.ops import STREAMS, Tick
from shopstream_generator.sinks.memory import MemoryCdcSink

Signature = list[tuple[int, int, list[bytes]]]


@dataclass(frozen=True)
class World:
    """A finished run: its ticks, every stream's canonical lines, its manifest and its sink."""

    ticks: list[Tick]
    lines: dict[str, list[bytes]]
    manifest: Manifest
    sink: MemoryCdcSink


def run_world(config: ModelConfig, until: int | None = None) -> World:
    raise NotImplementedError


def run_ticks(config: ModelConfig, until: int | None = None) -> list[Tick]:
    """Every tick of `[start, until)`; `until` defaults to the range end."""
    engine = Engine.new(config)
    return list(engine.run_until(config.end_us if until is None else until))


def lines_by_stream(ticks: Sequence[Tick]) -> dict[str, list[bytes]]:
    """Each stream's canonical lines in emission order, over all seven streams."""
    hasher = StreamHasher(keep_lines=True)
    for tick in ticks:
        hasher.add_tick(tick)
    return {name: hasher.lines(name) for name in STREAMS}


def signature(ticks: Sequence[Tick]) -> Signature:
    """`(seq, ts, canonical lines)` per tick: what two runs must agree on."""
    return [(tick.seq, tick.ts_us, [canon.line(tick.seq, op) for op in tick.ops]) for tick in ticks]


def run_chunked(config: ModelConfig, untils: Sequence[int]) -> list[Tick]:
    """A fake pacer: `run_until` over each of `untils` in order, then to the range end."""
    engine = Engine.new(config)
    ticks: list[Tick] = []
    for until in untils:
        ticks.extend(engine.run_until(until))
    ticks.extend(engine.run_until(config.end_us))
    return ticks


def run_resumed(
    config: ModelConfig, kill_at: int, until: int | None = None
) -> tuple[list[Tick], dict[str, object]]:
    """Run to `kill_at`, serialize to JSON text, resume from that text, and continue to `until`.

    Returns the ticks of both halves and the resumed engine's final serialized state.
    """
    first = Engine.new(config)
    ticks = list(first.run_until(kill_at))
    text = json.dumps(first.state.to_json())
    second = Engine.resume(config, json.loads(text))
    ticks.extend(second.run_until(config.end_us if until is None else until))
    return ticks, second.state.to_json()


def run_with_state(
    config: ModelConfig, until: int | None = None
) -> tuple[list[Tick], dict[str, object]]:
    """An uninterrupted run and its final serialized state."""
    engine = Engine.new(config)
    ticks = list(engine.run_until(config.end_us if until is None else until))
    return ticks, engine.state.to_json()
