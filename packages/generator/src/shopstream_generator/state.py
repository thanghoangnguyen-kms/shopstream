"""Everything the engine remembers, in one mutable dataclass, and its exact JSON form (D-18, D-24).

`EngineState` holds all engine state and nothing else: no module globals, no closures over
state. It holds only ids, enum ints and integers, never text, so it is written out as plain JSON
sections and read back to an equal state.

The heaps hold `(due_us, seq, kind, a, b)` tuples of ints. `seq` is unique and monotonic, so
two items never compare past their second element and heap order never depends on a payload.

Serialized form (format 1)::

    {"format": 1,
     "meta": {"model_sha256", "last_ts_us", "tick_seq", "heap_seq", "next_ids": {...}},
     "rng": {"<stream name>": {"mt": [624 ints], "pos": int, "words": int}},
     "cdc": [[due_us, seq, kind, a, b], ...],      # sorted
     "events": [...],                              # sorted
     "world": {"<field>": [[record ints...], ...]}}  # each list sorted by id

A sorted list is a valid heap and `(due, seq)` is unique, so the pop order of a restored heap is
the same as the original's. The world is serialized by one generic rule, so a field a later plan
adds to `World` or to a record is covered without touching this module: each `World` field is a
dict of id to a record dataclass whose first field is its id, and a record is the list of its
field values in declaration order. A value is an int, or None where the field allows it.

`from_json` never trusts its input: anything off raises `StateError` naming the section, and the
message never carries a value. The state of a different model (another seed, volume or rate) is
refused through `model_sha256`; a different range end or speed is accepted (D-24).
"""

from __future__ import annotations

import dataclasses
import heapq
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, get_args, get_type_hints

from .config import ModelConfig
from .rng import Stream, StreamName

HeapItem = tuple[int, int, int, int, int]

CUSTOMER = "customer"

FORMAT = 1
_HEAP_FIELDS = 5


class StateError(ValueError):
    """The serialized state is not one this engine can resume; the message names a section."""


@dataclass
class CustomerRec:
    """A customer as ints and None: text is rebuilt from the word lists at the row boundary.

    The email has its own indices and version, so a later name edit does not change it
    (Type 1 columns change independently).
    """

    customer_id: int
    created_us: int
    deleted_us: int | None
    first_idx: int
    last_idx: int
    email_first_idx: int
    email_last_idx: int
    email_version: int
    country_idx: int
    city_idx: int


@dataclass
class World:
    """The live business: ids and integers only."""

    customers: dict[int, CustomerRec] = field(default_factory=dict)


@dataclass
class EngineState:
    model_sha256: str
    last_ts_us: int
    tick_seq: int
    heap_seq: int
    next_ids: dict[str, int]
    streams: dict[StreamName, Stream]
    cdc: list[HeapItem]
    events: list[HeapItem]
    world: World

    @classmethod
    def new(cls, config: ModelConfig) -> EngineState:
        """A fresh state: the first tick may land exactly at the range start."""
        return cls(
            model_sha256=config.model_sha256(),
            last_ts_us=config.start_us - 1,
            tick_seq=0,
            heap_seq=0,
            next_ids={CUSTOMER: 1},
            streams={name: Stream(config.seed, name) for name in StreamName},
            cdc=[],
            events=[],
            world=World(),
        )

    def to_json(self) -> dict[str, object]:
        """The whole state as ints, strings, None, lists and string-keyed objects."""
        return {
            "format": FORMAT,
            "meta": {
                "model_sha256": self.model_sha256,
                "last_ts_us": self.last_ts_us,
                "tick_seq": self.tick_seq,
                "heap_seq": self.heap_seq,
                "next_ids": {name: self.next_ids[name] for name in sorted(self.next_ids)},
            },
            "rng": {name.value: self.streams[name].dump() for name in StreamName},
            "cdc": sorted(list(item) for item in self.cdc),
            "events": sorted(list(item) for item in self.events),
            "world": {
                f.name: _records_to_json(getattr(self.world, f.name))
                for f in dataclasses.fields(World)
            },
        }

    @classmethod
    def from_json(cls, data: object, config: ModelConfig) -> EngineState:
        """Rebuild a state from `to_json` output, or raise StateError."""
        top = _section(data, "state", {"format", "meta", "rng", "cdc", "events", "world"})
        if type(top["format"]) is not int or top["format"] != FORMAT:
            raise StateError(f"state.format: expected {FORMAT}")
        meta = _section(
            top["meta"],
            "meta",
            {"model_sha256", "last_ts_us", "tick_seq", "heap_seq", "next_ids"},
        )
        if meta["model_sha256"] != config.model_sha256():
            raise StateError("meta.model_sha256: the state belongs to a different model")
        ids = _section_any(meta["next_ids"], "meta.next_ids")
        next_ids = {name: _int(value, f"meta.next_ids.{name}") for name, value in ids.items()}
        rng = _section(top["rng"], "rng", {name.value for name in StreamName})
        streams: dict[StreamName, Stream] = {}
        for name in StreamName:
            try:
                streams[name] = Stream.load(config.seed, name, _section_any(rng[name.value], name))
            except ValueError:
                raise StateError(f"rng.{name.value}: the stream state is malformed") from None
        cdc = _heap(top["cdc"], "cdc")
        events = _heap(top["events"], "events")
        return cls(
            model_sha256=config.model_sha256(),
            last_ts_us=_int(meta["last_ts_us"], "meta.last_ts_us"),
            tick_seq=_int(meta["tick_seq"], "meta.tick_seq"),
            heap_seq=_int(meta["heap_seq"], "meta.heap_seq"),
            next_ids=next_ids,
            streams=streams,
            cdc=cdc,
            events=events,
            world=_world_from_json(top["world"]),
        )


# ---------------------------------------------------------------- parsing helpers


def _section_any(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise StateError(f"{path}: expected an object")
    return value


def _section(value: object, path: str, keys: set[str]) -> Mapping[str, object]:
    section = _section_any(value, path)
    if set(section) != keys:
        raise StateError(f"{path}: expected exactly the keys {', '.join(sorted(keys))}")
    return section


def _int(value: object, path: str) -> int:
    if type(value) is not int:
        raise StateError(f"{path}: expected an integer")
    return value


def _heap(value: object, path: str) -> list[HeapItem]:
    if not isinstance(value, list):
        raise StateError(f"{path}: expected a list")
    items: list[HeapItem] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, list) or len(raw) != _HEAP_FIELDS:
            raise StateError(f"{path}[{index}]: expected {_HEAP_FIELDS} integers")
        due, seq, kind, a, b = (_int(part, f"{path}[{index}]") for part in raw)
        items.append((due, seq, kind, a, b))
    heapq.heapify(items)
    return items


def _record_types() -> dict[str, Any]:
    """Each World field's record class, read from its `dict[int, Record]` annotation."""
    hints = get_type_hints(World)
    return {f.name: get_args(hints[f.name])[1] for f in dataclasses.fields(World)}


def _records_to_json(records: Mapping[int, Any]) -> list[list[int | None]]:
    """Records sorted by id (their first field), each as its field values in order."""
    rows: list[list[int | None]] = []
    for record in records.values():
        rows.append([getattr(record, f.name) for f in dataclasses.fields(record)])
    return sorted(rows, key=_row_id)


def _row_id(row: list[int | None]) -> int:
    first = row[0]
    return first if first is not None else 0


def _world_from_json(value: object) -> World:
    types = _record_types()
    section = _section(value, "world", set(types))
    tables: dict[str, dict[int, Any]] = {}
    for name, record_type in types.items():
        hints = get_type_hints(record_type)
        fields = dataclasses.fields(record_type)
        nullable = {f.name: type(None) in get_args(hints[f.name]) for f in fields}
        rows = section[name]
        if not isinstance(rows, list):
            raise StateError(f"world.{name}: expected a list")
        table: dict[int, Any] = {}
        for index, row in enumerate(rows):
            path = f"world.{name}[{index}]"
            if not isinstance(row, list) or len(row) != len(fields):
                raise StateError(f"{path}: expected {len(fields)} values")
            for f, cell in zip(fields, row, strict=True):
                if cell is None and nullable[f.name]:
                    continue
                if type(cell) is not int:
                    raise StateError(f"{path}.{f.name}: expected an integer")
            record = record_type(*row)
            record_id = row[0]
            if record_id in table:
                raise StateError(f"{path}: a record id is listed twice")
            table[record_id] = record
        tables[name] = table
    return World(**tables)
