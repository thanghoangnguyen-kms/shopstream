"""The throwaway load script for items 5 and 11 and the bronze trickle, run by the `cdc-run` one-shot.

ADR-001's Evidence rules exempt a one-off load script from unit tests, so this file has none; the
evidence page records its command line and its seed. It writes a seeded mix of transactions to the
five captured Postgres tables so that Debezium has changes to capture: inserts of new rows, updates
of live keys, and delete pairs (`UPDATE ... SET updated_at` then `DELETE` in one transaction, so a
delete's before image carries the simulated delete time, per ADR-001's Delete validity).

Modes: `item5` writes at `--rate` transactions per second for `--seconds` from one session; `trickle`
is the same mix at a low rate, touching only keys it created, with an id base derived from the seed so
it never writes a key an earlier run or an erasure ledger holds. `seed` and `item11` arrive with Plan
04-04 and are refused here with exit 2.

Every transaction is explicit, values go through psycopg parameters, and table and column names are
fixed constants checked with `read_v3.identifier`. A SimClock supplies the simulated business time
written to `updated_at`: it starts at 2026-01-01T00:00:00+00:00 and advances 1 ms per tick under a
lock. The script prints one JSON line, the last line of stdout, and never a generated email or name.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import os
import random
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

from read_v3 import error_text, identifier

TABLES: dict[str, tuple[str, ...]] = {
    "customers": ("customer_id",),
    "products": ("product_id",),
    "orders": ("order_id",),
    "order_items": ("order_id", "line_no"),
    "reviews": ("review_id",),
}
# The column an UPDATE changes, besides updated_at.
UPDATE_COLUMN = {
    "customers": "full_name",
    "products": "price",
    "orders": "status",
    "order_items": "quantity",
    "reviews": "body",
}
SIM_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
TICK = dt.timedelta(milliseconds=1)
INSERT_WEIGHT = 0.45
UPDATE_WEIGHT = 0.35
DEFAULT_ID_BASE = 1
TRICKLE_ID_BASE = 5_000_000
TRICKLE_ID_STEP = 100_000
DEFAULT_RATE = {"item5": 20.0, "trickle": 5.0}
STATUSES = ("new", "paid", "shipped", "done")
ACTIVE_MODES = ("item5", "trickle")
LATER_MODES = ("seed", "item11")
OWNER_LOGIN_ENV = "SHOPSTREAM_DB_PASSWORD"


class SimClock:
    """The simulated business clock: 1 ms per tick, safe to call from several sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = SIM_START
        self.start = SIM_START

    def tick(self) -> dt.datetime:
        with self._lock:
            self._now += TICK
            return self._now

    @property
    def end(self) -> dt.datetime:
        with self._lock:
            return self._now


class Keys:
    """The ids a session has created and still holds live, per table, plus the counters."""

    def __init__(self, id_base: int) -> None:
        self.counter = dict.fromkeys(TABLES, id_base)
        self.line_no = 0
        self.live: dict[str, list[tuple[int, ...]]] = {table: [] for table in TABLES}
        self.low: dict[str, int] = {}
        self.high: dict[str, int] = {}

    def note(self, table: str, key: tuple[int, ...]) -> None:
        first = key[0]
        self.low[table] = min(self.low.get(table, first), first)
        self.high[table] = max(self.high.get(table, first), first)

    def take(self, table: str, rng: random.Random, remove: bool) -> tuple[int, ...]:
        keys = self.live[table]
        index = rng.randrange(len(keys))
        if not remove:
            return keys[index]
        keys[index], keys[-1] = keys[-1], keys[index]
        return keys.pop()


def money(rng: random.Random, top: int) -> decimal.Decimal:
    """A synthetic price with two decimals, from 1.00 up to top / 100."""
    return decimal.Decimal(rng.randrange(100, top)).scaleb(-2)


def where(table: str) -> str:
    return " AND ".join(f"{identifier(column)} = %s" for column in TABLES[table])


def insert_row(conn: Any, table: str, keys: Keys, rng: random.Random, clock: SimClock) -> None:
    """Insert one new row with synthetic values; ids come from the per-table counters."""
    stamp = clock.tick()
    keys.counter[table] += 1
    new_id = keys.counter[table]
    if table == "customers":
        key: tuple[int, ...] = (new_id,)
        sql = "INSERT INTO customers (customer_id, email, full_name, updated_at) VALUES (%s, %s, %s, %s)"
        values: tuple[Any, ...] = (new_id, f"c{new_id}@example.test", f"customer {new_id}", stamp)
    elif table == "products":
        key = (new_id,)
        sql = "INSERT INTO products (product_id, name, price, updated_at) VALUES (%s, %s, %s, %s)"
        values = (new_id, f"product {new_id}", money(rng, 100000), stamp)
    elif table == "orders":
        key = (new_id,)
        sql = "INSERT INTO orders (order_id, customer_id, status, currency, updated_at) VALUES (%s, %s, %s, %s, %s)"
        values = (new_id, rng.randrange(1, new_id + 1), "new", "EUR", stamp)
    elif table == "order_items":
        keys.line_no += 1
        product = keys.take("products", rng, remove=False)[0] if keys.live["products"] else new_id
        key = (new_id, keys.line_no)
        sql = (
            "INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        values = (new_id, keys.line_no, product, rng.randrange(1, 10), money(rng, 10000), stamp)
    else:
        key = (new_id,)
        sql = (
            "INSERT INTO reviews (review_id, product_id, body, updated_at) VALUES (%s, %s, %s, %s)"
        )
        values = (new_id, rng.randrange(1, new_id + 1), f"review text {new_id}", stamp)
    with conn.transaction():
        conn.execute(sql, values)
    keys.live[table].append(key)
    keys.note(table, key)


def update_row(conn: Any, table: str, keys: Keys, rng: random.Random, clock: SimClock) -> None:
    key = keys.take(table, rng, remove=False)
    column = identifier(UPDATE_COLUMN[table])
    if table == "products":
        value: Any = money(rng, 100000)
    elif table == "orders":
        value = rng.choice(STATUSES)
    elif table == "order_items":
        value = rng.randrange(1, 10)
    else:
        value = f"{column} {rng.randrange(10**6)}"
    with conn.transaction():
        conn.execute(
            f"UPDATE {identifier(table)} SET {column} = %s, updated_at = %s WHERE {where(table)}",
            (value, clock.tick(), *key),
        )


def delete_row(conn: Any, table: str, keys: Keys, rng: random.Random, clock: SimClock) -> None:
    """A delete pair in one transaction: stamp updated_at, then DELETE the same key."""
    key = keys.take(table, rng, remove=True)
    with conn.transaction():
        conn.execute(
            f"UPDATE {identifier(table)} SET updated_at = %s WHERE {where(table)}",
            (clock.tick(), *key),
        )
        conn.execute(f"DELETE FROM {identifier(table)} WHERE {where(table)}", key)


def one_transaction(conn: Any, keys: Keys, rng: random.Random, clock: SimClock) -> tuple[str, str]:
    """Run one transaction by weight; return (table, verb). A table with no live key inserts."""
    table = rng.choice(sorted(TABLES))
    roll = rng.random()
    if roll < INSERT_WEIGHT or not keys.live[table]:
        insert_row(conn, table, keys, rng, clock)
        return table, "insert"
    if roll < INSERT_WEIGHT + UPDATE_WEIGHT:
        update_row(conn, table, keys, rng, clock)
        return table, "update"
    delete_row(conn, table, keys, rng, clock)
    return table, "delete"


def connect() -> Any:
    import psycopg

    password = os.environ.get(OWNER_LOGIN_ENV)
    if not password:
        raise ValueError(f"{OWNER_LOGIN_ENV} is not set")
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        dbname="shopstream",
        user="shopstream",
        password=password,
        autocommit=True,
    )


def wal_lsn(conn: Any) -> str:
    return str(conn.execute("SELECT pg_current_wal_lsn()::text").fetchone()[0])


def run(args: argparse.Namespace, command: str) -> dict[str, Any]:
    rate = args.rate if args.rate is not None else DEFAULT_RATE[args.mode]
    if args.id_base is not None:
        id_base = args.id_base
    elif args.mode == "trickle":
        id_base = TRICKLE_ID_BASE + args.seed * TRICKLE_ID_STEP
    else:
        id_base = DEFAULT_ID_BASE
    clock = SimClock()
    rng = random.Random(args.seed)
    keys = Keys(id_base)
    counts = {table: {"insert": 0, "update": 0, "delete": 0} for table in TABLES}
    with connect() as conn:
        start_lsn = wal_lsn(conn)
        started = time.monotonic()
        deadline = started + args.seconds
        interval = 1.0 / rate
        due = started
        while time.monotonic() < deadline:
            table, verb = one_transaction(conn, keys, rng, clock)
            counts[table][verb] += 1
            due += interval
            pause = due - time.monotonic()
            if pause > 0:
                time.sleep(pause)
        elapsed = time.monotonic() - started
        end_lsn = wal_lsn(conn)
    return {
        "mode": args.mode,
        "seed": args.seed,
        "sessions": 1,
        "seconds": args.seconds,
        "rate": rate,
        "command": command,
        "counts": counts,
        "ids": {table: [keys.low[table], keys.high[table]] for table in sorted(keys.low)},
        "sim_clock": {"start": clock.start.isoformat(), "end": clock.end.isoformat()},
        "start_lsn": start_lsn,
        "end_lsn": end_lsn,
        "elapsed_s": round(elapsed, 3),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seeded CDC workload for items 5 and 11.")
    parser.add_argument("--mode", required=True, choices=(*ACTIVE_MODES, *LATER_MODES))
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--rate", type=float, default=None, help="transactions per second")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--id-base", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(given)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    if args.mode in LATER_MODES:
        print(f"mode {args.mode} is added by a later plan", file=sys.stderr)
        return 2
    if args.seconds <= 0 or (args.rate is not None and args.rate <= 0):
        print("--seconds and --rate must be positive", file=sys.stderr)
        return 2
    try:
        report = run(args, "python /app/cdc_workload.py " + " ".join(given))
    except Exception as exc:
        password = os.environ.get(OWNER_LOGIN_ENV, "")
        print(f"error: {error_text(exc, [password])}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
