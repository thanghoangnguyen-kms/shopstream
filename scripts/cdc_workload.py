"""The throwaway load script for items 5 and 11 and the bronze trickle, run by the `cdc-run` one-shot.

ADR-001's Evidence rules exempt a one-off load script from unit tests, so this file has none; the
evidence page records its command line and its seed. It writes a seeded mix of transactions to the
five captured Postgres tables so that Debezium has changes to capture: inserts of new rows, updates
of live keys, and delete pairs (`UPDATE ... SET updated_at` then `DELETE` in one transaction, so a
delete's before image carries the simulated delete time, per ADR-001's Delete validity).

Modes: `item5` writes at `--rate` transactions per second for `--seconds` from one session; `trickle`
is the same mix at a low rate, touching only keys it created, with an id base derived from the seed so
it never writes a key an earlier run or an erasure ledger holds. `seed` inserts the pre-existing rows
(`--rows N`: customers N, products N/2, orders N, order_items 2N, reviews N/10, ids from 1, 100 rows
per transaction) that Debezium's initial snapshot reads as `op=r` rows. `item11` is the measured run:
`--sessions S` writer threads share one SimClock and together run `--rate` transactions per second for
`--seconds`; each statement takes its simulated timestamp from the clock and then waits a seeded 0 to
20 ms before it executes, so commit order can differ from timestamp order. Updates and delete pairs
pick from a hot set of 200 keys per table (the first 200 keys in the table, seeded keys included),
and inserts use new ids from 100,000. The knob flags of `item11`: `--alter-at T` runs `ALTER TABLE
customers ADD COLUMN tier text` at T seconds, `--canary` inserts one customer whose full_name is
CANARY_TOKEN (read from the environment, never printed), `--late-products K` inserts K order_items
rows whose product arrives in a later transaction, and `--review-injection` inserts one review whose
body is the neutral marker INJECTION_TEXT.

Every transaction is explicit, values go through psycopg parameters, and table and column names are
fixed constants checked with `read_v3.identifier`. A SimClock supplies the simulated business time
written to `updated_at`: it starts 1 s after the newest `updated_at` in the five tables (or at
2026-01-01T00:00:00+00:00 when they are empty) and advances 1 ms per tick under a lock. The script
prints one JSON line, the last line of stdout, and never a generated email or name.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import os
import random
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
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
CLOCK_GAP = dt.timedelta(seconds=1)
INSERT_WEIGHT = 0.45
UPDATE_WEIGHT = 0.35
DEFAULT_ID_BASE = 1
TRICKLE_ID_BASE = 5_000_000
TRICKLE_ID_STEP = 100_000
DEFAULT_RATE = {"item5": 20.0, "trickle": 5.0, "item11": 10.0}
STATUSES = ("new", "paid", "shipped", "done")
MODES = ("item5", "trickle", "seed", "item11")
OWNER_LOGIN_ENV = "SHOPSTREAM_DB_PASSWORD"
CANARY_ENV = "CANARY_TOKEN"
SEED_BATCH = 100
HOT_SIZE = 200
NEW_ID_BASE = 100_000
LATE_PRODUCT_BASE = 9_000_000
MAX_PAUSE_S = 0.020
TIERS = ("bronze", "silver", "gold")
DEBEZIUM_SLOT = "shopstream_dbz"
SLOT_WAIT_S = 120.0
# A neutral, descriptive marker with no imperative wording: the adversarial content itself is Week 3's
# generator's, and the knob only needs a known row and a known hash in bronze.
INJECTION_TEXT = (
    "[shopstream knob marker: seeded prompt-injection review, v1] "
    "This review stands in for adversarial text addressed to an AI assistant."
)


class SimClock:
    """The simulated business clock: 1 ms per tick, safe to call from several sessions."""

    def __init__(self, start: dt.datetime = SIM_START) -> None:
        self._lock = threading.Lock()
        self._now = start
        self.start = start

    def tick(self) -> dt.datetime:
        with self._lock:
            self._now += TICK
            return self._now

    @property
    def end(self) -> dt.datetime:
        with self._lock:
            return self._now


class SessionClock:
    """One session's view of the shared SimClock: take the tick, then wait 0 to 20 ms.

    The caller evaluates `tick()` while building a statement's parameters, so the simulated time is
    taken at statement time and the statement executes after the wait. Another session can take a
    later tick and commit first: that is how three sessions make commit order differ from timestamp
    order.
    """

    def __init__(self, clock: SimClock, rng: random.Random) -> None:
        self.clock = clock
        self.rng = rng

    def tick(self) -> dt.datetime:
        stamp = self.clock.tick()
        time.sleep(self.rng.random() * MAX_PAUSE_S)
        return stamp


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

    def next_id(self, table: str) -> int:
        self.counter[table] += 1
        return self.counter[table]

    def next_line(self) -> int:
        self.line_no += 1
        return self.line_no

    def has_live(self, table: str) -> bool:
        return bool(self.live[table])

    def add(self, table: str, key: tuple[int, ...]) -> None:
        self.live[table].append(key)
        self.note(table, key)

    def take(self, table: str, rng: random.Random, remove: bool) -> tuple[int, ...]:
        keys = self.live[table]
        index = rng.randrange(len(keys))
        if not remove:
            return keys[index]
        keys[index], keys[-1] = keys[-1], keys[index]
        return keys.pop()


class HotKeys(Keys):
    """The keys the item 11 sessions share: locked ids and a hot set of at most `size` keys per table.

    Updates and delete pairs pick from the hot set, so sessions collide on rows. A delete removes its
    key from the set, so only one session deletes a key; a new insert joins the set while it has room,
    which keeps it near `size`. A pick from an empty set names a key that does not exist (-1), so the
    statement changes no row.
    """

    def __init__(
        self, id_base: int, hot: dict[str, list[tuple[int, ...]]], size: int = HOT_SIZE
    ) -> None:
        super().__init__(id_base)
        self._lock = threading.Lock()
        self.live = hot
        self.size = size

    def next_id(self, table: str) -> int:
        with self._lock:
            return super().next_id(table)

    def next_line(self) -> int:
        with self._lock:
            return super().next_line()

    def has_live(self, table: str) -> bool:
        with self._lock:
            return super().has_live(table)

    def add(self, table: str, key: tuple[int, ...]) -> None:
        with self._lock:
            self.note(table, key)
            if len(self.live[table]) < self.size:
                self.live[table].append(key)

    def take(self, table: str, rng: random.Random, remove: bool) -> tuple[int, ...]:
        with self._lock:
            if not self.live[table]:
                return (-1,) * len(TABLES[table])
            return super().take(table, rng, remove)


def money(rng: random.Random, top: int) -> decimal.Decimal:
    """A synthetic price with two decimals, from 1.00 up to top / 100."""
    return decimal.Decimal(rng.randrange(100, top)).scaleb(-2)


def where(table: str) -> str:
    return " AND ".join(f"{identifier(column)} = %s" for column in TABLES[table])


def insert_row(
    conn: Any,
    table: str,
    keys: Keys,
    rng: random.Random,
    clock: SimClock | SessionClock,
    tier: bool = False,
) -> None:
    """Insert one new row with synthetic values; ids come from the per-table counters.

    With `tier`, a customer also sets the `tier` column (item 11's ALTER TABLE must have run).
    """
    stamp = clock.tick()
    new_id = keys.next_id(table)
    if table == "customers":
        key: tuple[int, ...] = (new_id,)
        sql = "INSERT INTO customers (customer_id, email, full_name, updated_at) VALUES (%s, %s, %s, %s)"
        values: tuple[Any, ...] = (new_id, f"c{new_id}@example.test", f"customer {new_id}", stamp)
        if tier:
            sql = (
                "INSERT INTO customers (customer_id, email, full_name, updated_at, tier) "
                "VALUES (%s, %s, %s, %s, %s)"
            )
            values = (*values, rng.choice(TIERS))
    elif table == "products":
        key = (new_id,)
        sql = "INSERT INTO products (product_id, name, price, updated_at) VALUES (%s, %s, %s, %s)"
        values = (new_id, f"product {new_id}", money(rng, 100000), stamp)
    elif table == "orders":
        key = (new_id,)
        sql = "INSERT INTO orders (order_id, customer_id, status, currency, updated_at) VALUES (%s, %s, %s, %s, %s)"
        values = (new_id, rng.randrange(1, new_id + 1), "new", "EUR", stamp)
    elif table == "order_items":
        line_no = keys.next_line()
        product = (
            keys.take("products", rng, remove=False)[0] if keys.has_live("products") else new_id
        )
        key = (new_id, line_no)
        sql = (
            "INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        values = (new_id, line_no, product, rng.randrange(1, 10), money(rng, 10000), stamp)
    else:
        key = (new_id,)
        sql = (
            "INSERT INTO reviews (review_id, product_id, body, updated_at) VALUES (%s, %s, %s, %s)"
        )
        values = (new_id, rng.randrange(1, new_id + 1), f"review text {new_id}", stamp)
    with conn.transaction():
        conn.execute(sql, values)
    keys.add(table, key)


def update_row(
    conn: Any, table: str, keys: Keys, rng: random.Random, clock: SimClock | SessionClock
) -> None:
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


def delete_row(
    conn: Any, table: str, keys: Keys, rng: random.Random, clock: SimClock | SessionClock
) -> None:
    """A delete pair in one transaction: stamp updated_at, then DELETE the same key."""
    key = keys.take(table, rng, remove=True)
    with conn.transaction():
        conn.execute(
            f"UPDATE {identifier(table)} SET updated_at = %s WHERE {where(table)}",
            (clock.tick(), *key),
        )
        conn.execute(f"DELETE FROM {identifier(table)} WHERE {where(table)}", key)


def one_transaction(
    conn: Any, keys: Keys, rng: random.Random, clock: SimClock | SessionClock, tier: bool = False
) -> tuple[str, str]:
    """Run one transaction by weight; return (table, verb). A table with no live key inserts."""
    table = rng.choice(sorted(TABLES))
    roll = rng.random()
    if roll < INSERT_WEIGHT or not keys.has_live(table):
        insert_row(conn, table, keys, rng, clock, tier=tier)
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


def clock_start(conn: Any) -> dt.datetime:
    """1 s after the newest updated_at in the five tables, or SIM_START when they are empty."""
    parts = " UNION ALL ".join(
        f"SELECT max(updated_at) AS newest FROM {identifier(table)}" for table in TABLES
    )
    newest = conn.execute(f"SELECT max(newest) FROM ({parts}) AS tables_newest").fetchone()[0]
    return SIM_START if newest is None else newest + CLOCK_GAP


def report_base(args: argparse.Namespace, command: str, clock: SimClock) -> dict[str, Any]:
    return {
        "mode": args.mode,
        "seed": args.seed,
        "command": command,
        "sim_clock": {"start": clock.start.isoformat(), "end": clock.end.isoformat()},
    }


def run_stream(args: argparse.Namespace, command: str) -> dict[str, Any]:
    """The item5 and trickle modes: one session at `--rate` transactions per second."""
    rate = args.rate if args.rate is not None else DEFAULT_RATE[args.mode]
    if args.id_base is not None:
        id_base = args.id_base
    elif args.mode == "trickle":
        id_base = TRICKLE_ID_BASE + args.seed * TRICKLE_ID_STEP
    else:
        id_base = DEFAULT_ID_BASE
    rng = random.Random(args.seed)
    keys = Keys(id_base)
    counts = {table: {"insert": 0, "update": 0, "delete": 0} for table in TABLES}
    with connect() as conn:
        clock = SimClock(clock_start(conn))
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
        **report_base(args, command, clock),
        "sessions": 1,
        "seconds": args.seconds,
        "rate": rate,
        "counts": counts,
        "ids": {table: [keys.low[table], keys.high[table]] for table in sorted(keys.low)},
        "start_lsn": start_lsn,
        "end_lsn": end_lsn,
        "elapsed_s": round(elapsed, 3),
    }


def seed_statements(
    rows: int, rng: random.Random, clock: SimClock
) -> Iterator[tuple[str, str, tuple[Any, ...]]]:
    """(table, SQL, parameters) for every seeded row, one clock tick per row, table by table."""
    products = rows // 2
    reviews = rows // 10
    for i in range(1, rows + 1):
        yield (
            "customers",
            "INSERT INTO customers (customer_id, email, full_name, updated_at) VALUES (%s, %s, %s, %s)",
            (i, f"c{i}@example.test", f"customer {i}", clock.tick()),
        )
    for i in range(1, products + 1):
        yield (
            "products",
            "INSERT INTO products (product_id, name, price, updated_at) VALUES (%s, %s, %s, %s)",
            (i, f"product {i}", money(rng, 100000), clock.tick()),
        )
    for i in range(1, rows + 1):
        yield (
            "orders",
            "INSERT INTO orders (order_id, customer_id, status, currency, updated_at) VALUES (%s, %s, %s, %s, %s)",
            (i, rng.randrange(1, rows + 1), "new", "EUR", clock.tick()),
        )
    for j in range(2 * rows):
        yield (
            "order_items",
            "INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                j // 2 + 1,
                j % 2 + 1,
                rng.randrange(1, max(products, 1) + 1),
                rng.randrange(1, 10),
                money(rng, 10000),
                clock.tick(),
            ),
        )
    for i in range(1, reviews + 1):
        yield (
            "reviews",
            "INSERT INTO reviews (review_id, product_id, body, updated_at) VALUES (%s, %s, %s, %s)",
            (i, rng.randrange(1, max(products, 1) + 1), f"review text {i}", clock.tick()),
        )


def run_seed(args: argparse.Namespace, command: str) -> dict[str, Any]:
    """The seed mode: the pre-existing rows Debezium's initial snapshot will read as op=r."""
    rng = random.Random(args.seed)
    seeded = dict.fromkeys(TABLES, 0)
    with connect() as conn:
        clock = SimClock(clock_start(conn))
        start_lsn = wal_lsn(conn)
        started = time.monotonic()
        batch: list[tuple[str, str, tuple[Any, ...]]] = []

        def flush() -> None:
            if not batch:
                return
            with conn.transaction():
                for _table, sql, values in batch:
                    conn.execute(sql, values)
            for table, _sql, _values in batch:
                seeded[table] += 1
            batch.clear()

        for statement in seed_statements(args.rows, rng, clock):
            if batch and (batch[0][0] != statement[0] or len(batch) >= SEED_BATCH):
                flush()
            batch.append(statement)
        flush()
        elapsed = time.monotonic() - started
        end_lsn = wal_lsn(conn)
    return {
        **report_base(args, command, clock),
        "sessions": 1,
        "rows": args.rows,
        "seeded_keys": seeded,
        "ids": {
            "customers": [1, args.rows],
            "products": [1, args.rows // 2],
            "orders": [1, args.rows],
            "order_items": [1, args.rows],
            "reviews": [1, args.rows // 10],
        },
        "start_lsn": start_lsn,
        "end_lsn": end_lsn,
        "elapsed_s": round(elapsed, 3),
    }


def first_keys(conn: Any, table: str, limit: int) -> list[tuple[int, ...]]:
    """The first `limit` primary keys of `table`, in key order."""
    columns = ", ".join(identifier(column) for column in TABLES[table])
    rows = conn.execute(
        f"SELECT {columns} FROM {identifier(table)} ORDER BY {columns} LIMIT %s", (limit,)
    ).fetchall()
    return [tuple(int(value) for value in row) for row in rows]


def wait_for_slot(conn: Any) -> bool:
    """Wait for Debezium's replication slot, so every change this run makes is one it streams.

    A change committed before the slot exists would reach bronze as a snapshot row instead of a
    streamed change. The connector creates the slot at the start of its initial snapshot, so the run
    still overlaps the snapshot's reading of the seeded rows.
    """
    deadline = time.monotonic() + SLOT_WAIT_S
    while time.monotonic() < deadline:
        found = conn.execute(
            "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (DEBEZIUM_SLOT,)
        ).fetchone()
        if found is not None:
            return True
        time.sleep(0.5)
    return False


def insert_canary(conn: Any, keys: Keys, clock: SessionClock, token: str, tier: bool) -> None:
    """One customer whose full_name is the canary token. The token is never printed or returned."""
    stamp = clock.tick()
    new_id = keys.next_id("customers")
    columns, marks = "customer_id, email, full_name, updated_at", "%s, %s, %s, %s"
    values: tuple[Any, ...] = (new_id, f"c{new_id}@example.test", token, stamp)
    if tier:
        columns, marks, values = columns + ", tier", marks + ", %s", (*values, "gold")
    with conn.transaction():
        conn.execute(f"INSERT INTO customers ({columns}) VALUES ({marks})", values)


def insert_late_item(
    conn: Any, keys: Keys, rng: random.Random, clock: SessionClock, product: int
) -> None:
    """An order_items row for a product that does not exist yet (the table has no foreign key)."""
    stamp = clock.tick()
    order_id = keys.next_id("order_items")
    line_no = keys.next_line()
    with conn.transaction():
        conn.execute(
            "INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (order_id, line_no, product, rng.randrange(1, 10), money(rng, 10000), stamp),
        )


def insert_late_product(conn: Any, rng: random.Random, clock: SessionClock, product: int) -> None:
    stamp = clock.tick()
    with conn.transaction():
        conn.execute(
            "INSERT INTO products (product_id, name, price, updated_at) VALUES (%s, %s, %s, %s)",
            (product, f"product {product}", money(rng, 100000), stamp),
        )


def insert_injection(conn: Any, keys: Keys, rng: random.Random, clock: SessionClock) -> int:
    """One review whose body is INJECTION_TEXT, stored as plain data and never interpreted."""
    stamp = clock.tick()
    review_id = keys.next_id("reviews")
    with conn.transaction():
        conn.execute(
            "INSERT INTO reviews (review_id, product_id, body, updated_at) VALUES (%s, %s, %s, %s)",
            (review_id, rng.randrange(1, 100), INJECTION_TEXT, stamp),
        )
    return review_id


def session_loop(
    index: int,
    args: argparse.Namespace,
    clock: SimClock,
    keys: Keys,
    canary: str | None,
    stop: threading.Event,
    started: float,
) -> dict[str, Any]:
    """One writer session; session 0 also runs the ALTER, the canary, the late products and the review."""
    rng = random.Random(args.seed + index)
    session_clock = SessionClock(clock, rng)
    counts = {table: {"insert": 0, "update": 0, "delete": 0} for table in TABLES}
    extra: dict[str, Any] = {
        "alter_done_at_s": None,
        "canary_rows": 0,
        "late_ids": [],
        "review_id": None,
    }
    rate = args.rate if args.rate is not None else DEFAULT_RATE["item11"]
    interval = args.sessions / rate
    deadline = started + args.seconds
    late_due = [
        (i + 1) * args.seconds / (args.late_products + 1) for i in range(args.late_products)
    ]
    pending: list[int] = []
    tier = False
    first = True
    try:
        with connect() as conn:
            due = time.monotonic()
            while time.monotonic() < deadline and not stop.is_set():
                if index == 0:
                    elapsed = time.monotonic() - started
                    for product in pending:
                        insert_late_product(conn, rng, session_clock, product)
                    pending = []
                    if first and canary is not None:
                        insert_canary(conn, keys, session_clock, canary, tier)
                        extra["canary_rows"] = 1
                    if first and args.review_injection:
                        extra["review_id"] = insert_injection(conn, keys, rng, session_clock)
                    if args.alter_at is not None and not tier and elapsed >= args.alter_at:
                        conn.execute("ALTER TABLE customers ADD COLUMN tier text")
                        tier = True
                        extra["alter_done_at_s"] = round(time.monotonic() - started, 3)
                        insert_row(conn, "customers", keys, rng, session_clock, tier=True)
                    if late_due and elapsed >= late_due[0]:
                        late_due.pop(0)
                        product = LATE_PRODUCT_BASE + len(extra["late_ids"])
                        insert_late_item(conn, keys, rng, session_clock, product)
                        extra["late_ids"].append(product)
                        pending.append(product)
                    first = False
                table, verb = one_transaction(conn, keys, rng, session_clock, tier=tier)
                counts[table][verb] += 1
                due += interval
                pause = due - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
            if index == 0:
                for product in pending:
                    insert_late_product(conn, rng, session_clock, product)
    except BaseException:
        stop.set()
        raise
    return {"counts": counts, **extra}


def run_item11(args: argparse.Namespace, command: str) -> dict[str, Any]:
    """The measured run: S writer sessions on a shared clock and a hot set, plus the knob seeds."""
    canary = os.environ.get(CANARY_ENV) if args.canary else None
    with connect() as conn:
        slot_seen = wait_for_slot(conn)
        clock = SimClock(clock_start(conn))
        hot = {table: first_keys(conn, table, HOT_SIZE) for table in TABLES}
        hot_start = {table: len(rows) for table, rows in hot.items()}
        start_lsn = wal_lsn(conn)
    keys = HotKeys(NEW_ID_BASE, hot)
    stop = threading.Event()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.sessions) as pool:
        futures = [
            pool.submit(session_loop, index, args, clock, keys, canary, stop, started)
            for index in range(args.sessions)
        ]
        results = [future.result() for future in futures]
    elapsed = time.monotonic() - started
    with connect() as conn:
        end_lsn = wal_lsn(conn)
    counts = {table: {"insert": 0, "update": 0, "delete": 0} for table in TABLES}
    for result in results:
        for table, verbs in result["counts"].items():
            for verb, number in verbs.items():
                counts[table][verb] += number
    lead = results[0]
    review_id = lead["review_id"]
    return {
        **report_base(args, command, clock),
        "sessions": args.sessions,
        "seconds": args.seconds,
        "rate": args.rate if args.rate is not None else DEFAULT_RATE["item11"],
        "counts": counts,
        "ids": {table: [keys.low[table], keys.high[table]] for table in sorted(keys.low)},
        "hot_keys": {**hot_start, "size": HOT_SIZE},
        "debezium_slot_seen": slot_seen,
        "alter": {
            "at_s": args.alter_at,
            "done": lead["alter_done_at_s"] is not None,
            "done_at_s": lead["alter_done_at_s"],
        },
        "canary_rows": lead["canary_rows"],
        "late_products": {"count": len(lead["late_ids"]), "product_ids": lead["late_ids"]},
        "review": (
            None
            if review_id is None
            else {
                "review_id": review_id,
                "body_sha256": hashlib.sha256(INJECTION_TEXT.encode()).hexdigest(),
            }
        ),
        "start_lsn": start_lsn,
        "end_lsn": end_lsn,
        "elapsed_s": round(elapsed, 3),
    }


def run(args: argparse.Namespace, command: str) -> dict[str, Any]:
    if args.mode == "seed":
        return run_seed(args, command)
    if args.mode == "item11":
        return run_item11(args, command)
    return run_stream(args, command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seeded CDC workload for items 5 and 11.")
    parser.add_argument("--mode", required=True, choices=MODES)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument(
        "--rate", type=float, default=None, help="transactions per second, in total"
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--id-base", type=int, default=None)
    parser.add_argument("--rows", type=int, default=500, help="seed: customers (others scale)")
    parser.add_argument(
        "--sessions", type=int, default=1, help="item11: concurrent writer sessions"
    )
    parser.add_argument(
        "--alter-at", type=float, default=None, help="item11: ALTER TABLE at T seconds"
    )
    parser.add_argument("--canary", action="store_true", help="item11: insert the canary customer")
    parser.add_argument(
        "--late-products", type=int, default=0, help="item11: late-arriving products"
    )
    parser.add_argument(
        "--review-injection", action="store_true", help="item11: insert the marker review"
    )
    return parser


def usage_error(args: argparse.Namespace) -> str | None:
    """A message for an invalid flag combination, or None."""
    if args.seconds <= 0 or (args.rate is not None and args.rate <= 0):
        return "--seconds and --rate must be positive"
    if args.rows < 1 or args.sessions < 1 or args.late_products < 0:
        return "--rows and --sessions must be at least 1 and --late-products at least 0"
    if args.alter_at is not None and args.alter_at < 0:
        return "--alter-at must not be negative"
    item11_only = (
        args.alter_at is not None
        or args.canary
        or args.late_products > 0
        or args.review_injection
        or args.sessions != 1
    )
    if item11_only and args.mode != "item11":
        return "--sessions, --alter-at, --canary, --late-products and --review-injection need --mode item11"
    if args.canary and not os.environ.get(CANARY_ENV):
        return f"--canary needs {CANARY_ENV} in the environment"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(given)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    problem = usage_error(args)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    try:
        report = run(args, "python /app/cdc_workload.py " + " ".join(given))
    except Exception as exc:
        secrets = [os.environ.get(OWNER_LOGIN_ENV, ""), os.environ.get(CANARY_ENV, "")]
        print(f"error: {error_text(exc, secrets)}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
