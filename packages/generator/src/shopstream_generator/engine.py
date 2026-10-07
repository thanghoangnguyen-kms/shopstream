"""The discrete-event engine: a heap of due items and the tick rule (D-01 to D-04, D-14).

The CDC heap holds `(due_us, seq, kind, a, b)` tuples of ints. `run_until` looks at the first
item, works out the tick it would get (`clock.next_tick`), and stops before popping it if that
tick is at or after `until`: the item stays in the heap and nothing is flushed (D-02). So the
state at any `until` is a function of seed, config and `until` alone, never of chunking.

A handler returns a `Tick`, or None when its item resolves to nothing. Only a `Tick` advances
the clock and the tick sequence, and every tick carries at least one op (D-01).

An arrival reschedules itself from its due time, not from the tick it was shifted to, so
arrival times stay a renewal process that ties don't disturb (D-03).

Output paths never iterate a set or rely on dict order.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from enum import IntEnum

from . import clock
from .config import ModelConfig
from .ops import Op, OpKind, Table, Tick
from .rng import StreamName
from .state import CUSTOMER, CustomerRec, EngineState


class ItemKind(IntEnum):
    """Heap item kinds. The values persist in a checkpoint later: append, never renumber."""

    INITIAL_PRODUCT = 1
    INITIAL_CUSTOMER = 2
    CUSTOMER_ARRIVAL = 3


class Engine:
    def __init__(self, config: ModelConfig, state: EngineState) -> None:
        self.config = config
        self.state = state

    @classmethod
    def new(cls, config: ModelConfig) -> Engine:
        """A fresh engine with its initial customers and the first sign-up scheduled (D-14)."""
        engine = cls(config, EngineState.new(config))
        for index in range(config.volumes.initial_customers):
            engine.push(config.start_us + index, ItemKind.INITIAL_CUSTOMER)
        if config.volumes.customers_per_day > 0:
            engine.push(config.start_us + engine._customer_gap(), ItemKind.CUSTOMER_ARRIVAL)
        return engine

    @classmethod
    def resume(cls, config: ModelConfig, data: object) -> Engine:
        """An engine over a serialized state; raises StateError for another model's state (D-24)."""
        return cls(config, EngineState.from_json(data, config))

    def push(self, due_us: int, kind: ItemKind, a: int = 0, b: int = 0) -> None:
        """Add a CDC item; its monotonic `seq` breaks ties in push order."""
        state = self.state
        state.heap_seq += 1
        heapq.heappush(state.cdc, (due_us, state.heap_seq, int(kind), a, b))

    def _customer_gap(self) -> int:
        """A uniform gap in [1, 2 * mean] microseconds: integer-only, mean preserved (D-03)."""
        mean_gap_us = clock.US_PER_DAY // self.config.volumes.customers_per_day
        return self.state.streams[StreamName.CUSTOMERS].between(1, 2 * mean_gap_us)

    def run_until(self, until_us: int) -> Iterator[Tick]:
        """Yield every tick whose timestamp is before `until_us`, in order."""
        state = self.state
        while state.cdc:
            due_us = state.cdc[0][0]
            ts_us = clock.next_tick(due_us, state.last_ts_us)
            if ts_us >= until_us:
                return
            item = heapq.heappop(state.cdc)
            tick = self._dispatch(item[2], due_us, ts_us)
            if tick is not None:
                state.last_ts_us = ts_us
                state.tick_seq = tick.seq
                yield tick

    def _dispatch(self, kind: int, due_us: int, ts_us: int) -> Tick | None:
        if kind == ItemKind.INITIAL_CUSTOMER:
            return self._insert_customer(ts_us)
        if kind == ItemKind.CUSTOMER_ARRIVAL:
            self.push(due_us + self._customer_gap(), ItemKind.CUSTOMER_ARRIVAL)
            return self._insert_customer(ts_us)
        raise ValueError(f"no handler for item kind {kind}")

    def _insert_customer(self, ts_us: int) -> Tick:
        state = self.state
        customer_id = state.next_ids[CUSTOMER]
        state.next_ids[CUSTOMER] = customer_id + 1
        state.world.customers[customer_id] = CustomerRec(customer_id, ts_us, None)
        moment = clock.to_datetime(ts_us)
        op = Op(
            Table.CUSTOMERS,
            OpKind.INSERT,
            {"customer_id": customer_id},
            {
                "customer_id": customer_id,
                "created_at": moment,
                "updated_at": moment,
                "deleted_at": None,
            },
        )
        return Tick(state.tick_seq + 1, ts_us, (op,))
