"""MemoryCdcSink (D-25): raises wherever Postgres would, plus the clock rules Postgres can't see.

Each case is one hand-built tick, no engine, with exactly one defect. A case is either a
Postgres check (Postgres itself would reject it) or an oracle check (stricter than Postgres,
which would round, pad or accept it silently, or a rule it can't see: ADR-004 C2, C3, C6). A
raising tick leaves the sink exactly as it was, and no message carries a row value (T-02-15).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from shopstream_generator import clock, golden
from shopstream_generator import config as model_config
from shopstream_generator.config import ModelConfig
from shopstream_generator.engine import Engine
from shopstream_generator.ops import Op, OpKind, Table, Tick
from shopstream_generator.schema import SCHEMAS, ColumnType
from shopstream_generator.sinks.memory import CdcViolation, MemoryCdcSink

from . import harness
from .strategies import START

GOLDEN = Path(__file__).resolve().parent / "golden"
PLANTED = "planted-value-must-not-leak"


def at(n: int) -> int:
    """A tick instant, `n` milliseconds after the start."""
    return START + n * 1000


def stamp(created: int, updated: int) -> dict[str, object]:
    return {"created_at": clock.to_datetime(created), "updated_at": clock.to_datetime(updated)}


def customer(cid: int, created: int, updated: int | None = None, **over: Any) -> dict[str, object]:
    return {
        "customer_id": cid,
        "email": f"c{cid}@example.com",
        "full_name": "Ada Lovelace",
        "city": "Paris",
        "country": "FR",
        "deleted_at": None,
        **stamp(created, created if updated is None else updated),
        **over,
    }


def product(pid: int, created: int, updated: int | None = None, **over: Any) -> dict[str, object]:
    return {
        "product_id": pid,
        "name": "Sturdy Lamp",
        "category": "home",
        "list_price": Decimal("9.99"),
        "deleted_at": None,
        **stamp(created, created if updated is None else updated),
        **over,
    }


def order(oid: int, created: int, updated: int | None = None, **over: Any) -> dict[str, object]:
    return {
        "order_id": oid,
        "customer_id": 1,
        "status": "placed",
        "currency_code": "EUR",
        "order_discount": Decimal("0.00"),
        "ordered_at": clock.to_datetime(created),
        **stamp(created, created if updated is None else updated),
        **over,
    }


def item(
    oid: int, line: int, created: int, updated: int | None = None, **over: Any
) -> dict[str, object]:
    return {
        "order_id": oid,
        "line_number": line,
        "product_id": 1,
        "quantity": 2,
        "unit_price": Decimal("4.50"),
        **stamp(created, created if updated is None else updated),
        **over,
    }


def payment(pid: int, created: int, updated: int | None = None, **over: Any) -> dict[str, object]:
    return {
        "payment_id": pid,
        "order_id": 1,
        "payment_kind": "capture",
        "amount": Decimal("9.00"),
        "currency_code": "EUR",
        "payment_method": "card",
        "paid_at": clock.to_datetime(created),
        **stamp(created, created if updated is None else updated),
        **over,
    }


def review(rid: int, created: int, updated: int | None = None, **over: Any) -> dict[str, object]:
    return {
        "review_id": rid,
        "product_id": 1,
        "customer_id": 1,
        "rating": 4,
        "body": "Fine.",
        **stamp(created, created if updated is None else updated),
        **over,
    }


def ins(table: Table, key: Mapping[str, int], row: Mapping[str, object]) -> Op:
    return Op(table, OpKind.INSERT, key, row)


def upd(table: Table, key: Mapping[str, int], row: Mapping[str, object]) -> Op:
    return Op(table, OpKind.UPDATE, key, row)


def dele(table: Table, key: Mapping[str, int]) -> Op:
    return Op(table, OpKind.DELETE, key, None)


def tick(seq: int, ts: int, *ops: Op) -> Tick:
    return Tick(seq, ts, ops)


def cust(cid: int) -> dict[str, int]:
    return {"customer_id": cid}


def line(oid: int, number: int) -> dict[str, int]:
    return {"order_id": oid, "line_number": number}


Case = tuple[list[Tick], Tick]


def snapshot(sink: MemoryCdcSink) -> tuple[object, ...]:
    tables = {t.value: dict(sink.table(t)) for t in Table}
    return (tables, sink.last_seq, sink.last_ts_us)


# ---------------------------------------------------------------- one hand-built case per check


def _duplicate_pk() -> Case:
    return [tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))], tick(
        2, at(2), ins(Table.CUSTOMERS, cust(1), customer(1, at(2)))
    )


def _update_missing() -> Case:
    return [], tick(1, at(1), upd(Table.CUSTOMERS, cust(1), customer(1, at(0), at(1))))


def _delete_missing() -> Case:
    return [], tick(1, at(1), dele(Table.ORDER_ITEMS, line(1, 1)))


def _not_null() -> Case:
    return [], tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1), city=None)))


def _unknown_column() -> Case:
    return [], tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1), tier="gold")))


def _bool_for_bigint() -> Case:
    return [], tick(1, at(1), ins(Table.ORDERS, {"order_id": 1}, order(1, at(1), customer_id=True)))


def _char3_too_long() -> Case:
    return [], tick(
        1, at(1), ins(Table.ORDERS, {"order_id": 1}, order(1, at(1), currency_code="EURO"))
    )


def _amount_zero() -> Case:
    return [], tick(
        1,
        at(1),
        ins(Table.PAYMENTS, {"payment_id": 1}, payment(1, at(1), amount=Decimal("0.00"))),
    )


def _rating_six() -> Case:
    return [], tick(1, at(1), ins(Table.REVIEWS, {"review_id": 1}, review(1, at(1), rating=6)))


def _discount_negative() -> Case:
    return [], tick(
        1,
        at(1),
        ins(Table.ORDERS, {"order_id": 1}, order(1, at(1), order_discount=Decimal("-0.01"))),
    )


def _missing_column() -> Case:
    row = customer(1, at(1))
    del row["deleted_at"]
    return [], tick(1, at(1), ins(Table.CUSTOMERS, cust(1), row))


def _float_money() -> Case:
    return [], tick(
        1, at(1), ins(Table.PRODUCTS, {"product_id": 1}, product(1, at(1), list_price=9.99))
    )


def _numeric_scale() -> Case:
    return [], tick(
        1,
        at(1),
        ins(Table.PRODUCTS, {"product_id": 1}, product(1, at(1), list_price=Decimal("9.9"))),
    )


def _naive_timestamptz() -> Case:
    naive = datetime(2025, 6, 28, 0, 0, 0)
    return [], tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1), deleted_at=naive)))


def _key_mismatch() -> Case:
    return [], tick(1, at(1), ins(Table.CUSTOMERS, cust(2), customer(1, at(1))))


def _seq_gap() -> Case:
    return [], tick(2, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))


def _ts_not_rising() -> Case:
    return [tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))], tick(
        2, at(1), ins(Table.CUSTOMERS, cust(2), customer(2, at(1)))
    )


def _empty_tick() -> Case:
    return [], tick(1, at(1))


def _updated_at_not_tick() -> Case:
    return [tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))], tick(
        2, at(2), upd(Table.CUSTOMERS, cust(1), customer(1, at(1), at(1), city="Lyon"))
    )


def _created_at_not_tick() -> Case:
    row = customer(1, at(1), created_at=clock.to_datetime(at(0)))
    return [], tick(1, at(1), ins(Table.CUSTOMERS, cust(1), row))


def _created_at_changed() -> Case:
    return [tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))], tick(
        2, at(2), upd(Table.CUSTOMERS, cust(1), customer(1, at(5), at(2)))
    )


def _key_twice() -> Case:
    first = upd(Table.CUSTOMERS, cust(1), customer(1, at(1), at(2), city="Lyon"))
    second = upd(Table.CUSTOMERS, cust(1), customer(1, at(1), at(2), city="Nice"))
    return [tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))], tick(
        2, at(2), first, second
    )


def _delete_without_update() -> Case:
    return [tick(1, at(1), ins(Table.ORDER_ITEMS, line(1, 1), item(1, 1, at(1))))], tick(
        2, at(2), dele(Table.ORDER_ITEMS, line(1, 1))
    )


def _delete_on_orders() -> Case:
    return [tick(1, at(1), ins(Table.ORDERS, {"order_id": 1}, order(1, at(1))))], tick(
        2, at(2), dele(Table.ORDERS, {"order_id": 1})
    )


def _soft_deleted_customer_changed() -> Case:
    setup = [
        tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1)))),
        tick(
            2,
            at(2),
            upd(
                Table.CUSTOMERS,
                cust(1),
                customer(1, at(1), at(2), deleted_at=clock.to_datetime(at(2))),
            ),
        ),
    ]
    later = customer(1, at(1), at(3), deleted_at=clock.to_datetime(at(2)), city="Lyon")
    return setup, tick(3, at(3), upd(Table.CUSTOMERS, cust(1), later))


def _payment_updated() -> Case:
    return [tick(1, at(1), ins(Table.PAYMENTS, {"payment_id": 1}, payment(1, at(1))))], tick(
        2,
        at(2),
        upd(
            Table.PAYMENTS,
            {"payment_id": 1},
            payment(1, at(1), at(2), payment_method="paypal"),
        ),
    )


# (id, builder, table, check, kind): the 10 Postgres and 16 oracle cases
CASES: list[tuple[str, Callable[[], Case], Table | None, str, str]] = [
    ("duplicate-pk", _duplicate_pk, Table.CUSTOMERS, "duplicate-pk", "postgres"),
    ("update-missing", _update_missing, Table.CUSTOMERS, "update-missing", "postgres"),
    ("delete-missing", _delete_missing, Table.ORDER_ITEMS, "delete-missing", "postgres"),
    ("not-null", _not_null, Table.CUSTOMERS, "not-null", "postgres"),
    ("unknown-column", _unknown_column, Table.CUSTOMERS, "unknown-column", "postgres"),
    ("wrong-type-bool-for-bigint", _bool_for_bigint, Table.ORDERS, "wrong-type", "postgres"),
    ("char3-too-long", _char3_too_long, Table.ORDERS, "char3-length", "postgres"),
    ("check-amount-zero", _amount_zero, Table.PAYMENTS, "check-positive", "postgres"),
    ("check-rating-6", _rating_six, Table.REVIEWS, "check-between", "postgres"),
    ("check-discount-negative", _discount_negative, Table.ORDERS, "check-non-negative", "postgres"),
    ("missing-column", _missing_column, Table.CUSTOMERS, "missing-column", "oracle"),
    ("float-money", _float_money, Table.PRODUCTS, "numeric-type", "oracle"),
    ("numeric-scale", _numeric_scale, Table.PRODUCTS, "numeric-scale", "oracle"),
    ("naive-timestamptz", _naive_timestamptz, Table.CUSTOMERS, "timestamptz-utc", "oracle"),
    ("key-mismatch", _key_mismatch, Table.CUSTOMERS, "key-mismatch", "oracle"),
    ("seq-gap", _seq_gap, None, "seq-gap", "oracle"),
    ("ts-not-rising", _ts_not_rising, None, "ts-not-rising", "oracle"),
    ("empty-tick", _empty_tick, None, "empty-tick", "oracle"),
    ("updated-at-not-tick", _updated_at_not_tick, Table.CUSTOMERS, "updated-at-not-tick", "oracle"),
    ("created-at-not-tick", _created_at_not_tick, Table.CUSTOMERS, "created-at-not-tick", "oracle"),
    ("created-at-changed", _created_at_changed, Table.CUSTOMERS, "created-at-changed", "oracle"),
    ("key-twice", _key_twice, Table.CUSTOMERS, "key-twice", "oracle"),
    (
        "delete-without-update",
        _delete_without_update,
        Table.ORDER_ITEMS,
        "delete-without-update",
        "oracle",
    ),
    ("delete-on-orders", _delete_on_orders, Table.ORDERS, "delete-not-allowed", "oracle"),
    (
        "soft-deleted-customer-changed",
        _soft_deleted_customer_changed,
        Table.CUSTOMERS,
        "soft-deleted-customer-changed",
        "oracle",
    ),
    ("payment-updated", _payment_updated, Table.PAYMENTS, "insert-only", "oracle"),
]


def committed(setup: list[Tick]) -> MemoryCdcSink:
    sink = MemoryCdcSink()
    for setup_tick in setup:
        sink.commit(setup_tick)
    return sink


def test_the_case_table_has_ten_postgres_and_sixteen_oracle_cases() -> None:
    kinds = [kind for _, _, _, _, kind in CASES]
    assert (kinds.count("postgres"), kinds.count("oracle")) == (10, 16)
    assert len({case_id for case_id, *_ in CASES}) == 26


@pytest.mark.parametrize(
    ("build", "table", "check", "kind"),
    [
        pytest.param(build, table, check, kind, id=case_id)
        for case_id, build, table, check, kind in CASES
    ],
)
def test_a_defective_tick_raises_the_named_violation_and_changes_nothing(
    build: Callable[[], Case], table: Table | None, check: str, kind: str
) -> None:
    setup, bad = build()
    sink = committed(setup)
    before = snapshot(sink)
    with pytest.raises(CdcViolation) as raised:
        sink.commit(bad)
    assert (raised.value.table, raised.value.check, raised.value.kind) == (table, check, kind)
    assert snapshot(sink) == before


# ---------------------------------------------------------------- clean commits and atomicity


def test_an_insert_an_update_and_a_delete_pair_commit_cleanly() -> None:
    sink = committed(
        [
            tick(
                1,
                at(1),
                ins(Table.CUSTOMERS, cust(1), customer(1, at(1))),
                ins(Table.ORDER_ITEMS, line(1, 1), item(1, 1, at(1))),
            )
        ]
    )
    second = tick(
        2,
        at(2),
        ins(Table.CUSTOMERS, cust(2), customer(2, at(2))),
        upd(Table.CUSTOMERS, cust(1), customer(1, at(1), at(2), city="Lyon")),
        upd(Table.ORDER_ITEMS, line(1, 1), item(1, 1, at(1), at(2), quantity=3)),
        dele(Table.ORDER_ITEMS, line(1, 1)),
    )
    sink.commit(second)
    assert (sink.last_seq, sink.last_ts_us) == (2, at(2))
    assert sorted(sink.table(Table.CUSTOMERS)) == [(1,), (2,)]
    assert sink.table(Table.CUSTOMERS)[(1,)]["city"] == "Lyon"
    assert dict(sink.table(Table.ORDER_ITEMS)) == {}


def test_a_tick_that_fails_on_its_last_op_applies_none_of_its_earlier_ops() -> None:
    sink = committed([tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))])
    before = snapshot(sink)
    bad = tick(
        2,
        at(2),
        ins(Table.CUSTOMERS, cust(2), customer(2, at(2))),
        upd(Table.CUSTOMERS, cust(1), customer(1, at(1), at(2), city="Lyon")),
        ins(Table.CUSTOMERS, cust(3), customer(3, at(2), city=None)),
    )
    with pytest.raises(CdcViolation):
        sink.commit(bad)
    assert snapshot(sink) == before
    sink.commit(tick(2, at(2), ins(Table.CUSTOMERS, cust(2), customer(2, at(2)))))
    assert sink.last_seq == 2


def test_the_table_view_is_read_only() -> None:
    sink = committed([tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))])
    view: Any = sink.table(Table.CUSTOMERS)
    with pytest.raises(TypeError):
        view[(9,)] = {}


# ---------------------------------------------------------------- messages never carry values


def test_a_message_names_table_check_kind_and_column_but_never_a_value() -> None:
    sink = MemoryCdcSink()
    bad_not_null = ins(Table.CUSTOMERS, cust(1), customer(1, at(1), email=PLANTED, city=None))
    with pytest.raises(CdcViolation) as first:
        sink.commit(tick(1, at(1), bad_not_null))
    text = str(first.value)
    assert PLANTED not in text
    for part in ("customers", "not-null", "postgres", "city"):
        assert part in text
    bad_type = ins(Table.ORDERS, {"order_id": 1}, order(1, at(1), customer_id=PLANTED))
    with pytest.raises(CdcViolation) as second:
        sink.commit(tick(1, at(1), bad_type))
    assert PLANTED not in str(second.value)
    assert second.value.column == "customer_id"


# ---------------------------------------------------------------- finer type rules


def test_integers_must_fit_their_column_and_text_must_be_text() -> None:
    cases = [
        (Table.ORDERS, {"order_id": 1}, order(1, at(1), customer_id=2**63)),
        (Table.ORDER_ITEMS, line(1, 1), item(1, 1, at(1), quantity=2**31)),
        (Table.ORDER_ITEMS, line(1, 1), item(1, 1, at(1), quantity=1.0)),
        (Table.CUSTOMERS, cust(1), customer(1, at(1), city=7)),
    ]
    for table, key, row in cases:
        with pytest.raises(CdcViolation) as raised:
            MemoryCdcSink().commit(tick(1, at(1), ins(table, key, row)))
        assert (raised.value.check, raised.value.kind) == ("wrong-type", "postgres")


def test_a_short_currency_code_is_an_oracle_violation_because_postgres_would_pad_it() -> None:
    row = order(1, at(1), currency_code="EU")
    with pytest.raises(CdcViolation) as raised:
        MemoryCdcSink().commit(tick(1, at(1), ins(Table.ORDERS, {"order_id": 1}, row)))
    assert (raised.value.check, raised.value.kind) == ("char3-length", "oracle")


def test_a_timestamp_must_be_aware_and_at_offset_zero() -> None:
    offset = datetime(2025, 6, 28, tzinfo=timezone(timedelta(hours=7)))
    for value in (offset, "2025-06-28T00:00:00Z"):
        row = customer(1, at(1), deleted_at=value)
        with pytest.raises(CdcViolation) as raised:
            MemoryCdcSink().commit(tick(1, at(1), ins(Table.CUSTOMERS, cust(1), row)))
        assert (raised.value.check, raised.value.kind) == ("timestamptz-utc", "oracle")


def test_a_decimal_must_be_finite_and_fit_eighteen_digits() -> None:
    for value in (Decimal("NaN"), Decimal("1" * 17 + ".00")):
        row = product(1, at(1), list_price=value)
        with pytest.raises(CdcViolation) as raised:
            MemoryCdcSink().commit(tick(1, at(1), ins(Table.PRODUCTS, {"product_id": 1}, row)))
        assert raised.value.kind == "oracle"
        assert raised.value.check in {"numeric-type", "numeric-scale"}


def test_the_clock_rules_hold_for_a_valid_insert_and_update() -> None:
    sink = MemoryCdcSink()
    sink.commit(tick(1, at(1), ins(Table.PRODUCTS, {"product_id": 1}, product(1, at(1)))))
    sink.commit(
        tick(
            2,
            at(2),
            upd(Table.PRODUCTS, {"product_id": 1}, product(1, at(1), at(2), name="New Lamp")),
        )
    )
    assert sink.table(Table.PRODUCTS)[(1,)]["name"] == "New Lamp"
    assert sink.table(Table.PRODUCTS)[(1,)]["updated_at"] == clock.to_datetime(at(2))


# ---------------------------------------------------------------- the schema module


def test_schemas_declare_the_six_ref_section_7_tables() -> None:
    assert sorted(table.value for table in SCHEMAS) == [
        "customers",
        "order_items",
        "orders",
        "payments",
        "products",
        "reviews",
    ]
    keys = {table.value: schema.primary_key for table, schema in SCHEMAS.items()}
    assert keys["order_items"] == ("order_id", "line_number")
    assert keys["customers"] == ("customer_id",)
    for schema in SCHEMAS.values():
        columns = {column.name: column for column in schema.columns}
        for name in ("created_at", "updated_at"):
            assert columns[name].type is ColumnType.TIMESTAMPTZ
            assert not columns[name].nullable
        assert set(schema.primary_key) <= set(columns)
        assert all(not columns[name].nullable for name in schema.primary_key)
    assert {t.value for t, s in SCHEMAS.items() if s.delete_pairs} == {"order_items", "reviews"}
    assert {t.value for t, s in SCHEMAS.items() if s.insert_only} == {"payments"}
    checks = {
        (table.value, check.column, check.kind)
        for table, schema in SCHEMAS.items()
        for check in schema.checks
    }
    assert checks == {
        ("orders", "order_discount", "non_negative"),
        ("payments", "amount", "positive"),
        ("reviews", "rating", "between"),
    }


# ---------------------------------------------------------------- every run goes through the sink


def golden_config() -> ModelConfig:
    return model_config.load(GOLDEN / "config.json")


class CountingSink(MemoryCdcSink):
    """Counts commits across instances, so a test can see a path really used the sink."""

    commits = 0

    def commit(self, tick: Tick) -> None:
        type(self).commits += 1
        super().commit(tick)


def test_the_golden_run_commits_every_tick_to_the_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    config = golden_config()
    expected = len(list(Engine.new(config).run_until(config.end_us)))
    CountingSink.commits = 0
    monkeypatch.setattr(golden, "MemoryCdcSink", CountingSink)
    manifest = golden.build_manifest(config)
    assert CountingSink.commits == expected > 0
    assert manifest.to_bytes() == (GOLDEN / "manifest.json").read_bytes()


def test_the_golden_run_stops_on_a_defective_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Engine, until_us: int) -> Any:
        good = tick(1, at(1), ins(Table.CUSTOMERS, cust(1), customer(1, at(1))))
        yield good
        yield tick(2, at(2), ins(Table.CUSTOMERS, cust(1), customer(1, at(2))))

    monkeypatch.setattr(Engine, "run_until", broken)
    with pytest.raises(CdcViolation) as raised:
        golden.build_manifest(golden_config())
    assert raised.value.check == "duplicate-pk"


def test_every_harness_helper_commits_to_a_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    config = dataclasses.replace(
        golden_config(), end_us=golden_config().start_us + clock.US_PER_HOUR
    )
    total = len(harness.run_ticks(config))
    assert total > 0
    monkeypatch.setattr(harness, "MemoryCdcSink", CountingSink)
    runs: list[Callable[[], list[Tick]]] = [
        lambda: harness.run_ticks(config),
        lambda: harness.run_chunked(config, [config.start_us + 1000]),
        lambda: harness.run_resumed(config, config.start_us + 1000)[0],
        lambda: harness.run_with_state(config)[0],
        lambda: harness.run_world(config).ticks,
    ]
    for run in runs:
        CountingSink.commits = 0
        assert len(run()) == total
        assert CountingSink.commits == total


def test_run_world_returns_ticks_lines_manifest_and_the_sink() -> None:
    config = golden_config()
    world = harness.run_world(config)
    assert world.sink.last_seq == len(world.ticks) > 0
    assert world.sink.last_ts_us == world.ticks[-1].ts_us
    assert world.manifest.to_bytes() == (GOLDEN / "manifest.json").read_bytes()
    assert world.lines == harness.lines_by_stream(world.ticks)
    assert harness.run_world(config, until=config.start_us).ticks == []
