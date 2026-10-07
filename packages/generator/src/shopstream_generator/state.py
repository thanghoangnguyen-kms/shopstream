"""Everything the engine remembers, in one mutable dataclass (D-18, D-24).

`EngineState` holds all engine state and nothing else: no module globals, no closures over
state. It holds only ids, enum ints and integers, never text, so it can later be written out as
plain JSON sections and read back to an equal state. Phase 4's checkpoint serialization is
added without changing this shape.

The heaps hold `(due_us, seq, kind, a, b)` tuples of ints. `seq` is unique and monotonic, so
two items never compare past their second element and heap order never depends on a payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import ModelConfig
from .rng import Stream, StreamName

HeapItem = tuple[int, int, int, int, int]

CUSTOMER = "customer"


class StateError(ValueError):
    """The serialized state is not one this engine can resume."""


@dataclass
class CustomerRec:
    customer_id: int
    created_us: int
    deleted_us: int | None


@dataclass
class World:
    """The live business: ids and integers only."""

    customers: dict[int, CustomerRec] = field(default_factory=dict)


@dataclass
class EngineState:
    last_ts_us: int
    tick_seq: int
    heap_seq: int
    next_ids: dict[str, int]
    streams: dict[StreamName, Stream]
    cdc: list[HeapItem]
    events: list[HeapItem]
    world: World

    @classmethod
    def new(cls, seed: int, start_us: int) -> EngineState:
        """A fresh state: the first tick may land exactly at `start_us`."""
        return cls(
            last_ts_us=start_us - 1,
            tick_seq=0,
            heap_seq=0,
            next_ids={CUSTOMER: 1},
            streams={name: Stream(seed, name) for name in StreamName},
            cdc=[],
            events=[],
            world=World(),
        )

    def to_json(self) -> dict[str, object]:
        raise NotImplementedError("RED stub")

    @classmethod
    def from_json(cls, data: object, config: ModelConfig) -> EngineState:
        raise NotImplementedError("RED stub")
