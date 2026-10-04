"""Unit tests for scripts/clickstream_load.py: item 13's clickstream knobs.

No Kafka, Karapace or fastavro is needed for the pure parts: the knob plan, the events and the report
are built from the generator's own functions, with fake offsets in place of delivery reports, and the
canary is a token made by `secrets`. The encoding tests skip when fastavro (the spike group) is absent.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
from typing import Any

import clickstream_load as cl
import pytest
import throughput_check as tc

WORKERS = (0, 1)
HUNDRED_THOUSAND = 100_000


def knob_events(worker: int = 0, count: int = 1_000, canary: str | None = None) -> list[Any]:
    rng = random.Random(7 * 1000 + worker)
    out: list[Any] = []
    for n in range(count):
        out.extend(cl.records_for(rng, 7, worker, n, knobs=True, canary=canary))
    return out


# --- knob_of: fixed residues that never share an event ------------------------------------------


def test_residues_never_put_two_knobs_on_one_event() -> None:
    for worker in WORKERS:
        for n in range(1_000_000):
            assert len(cl.matching_knobs(n, worker)) <= 1, (worker, n)


def test_knob_of_names_the_one_knob_on_each_planned_event() -> None:
    assert cl.knob_of(13, 0) == "duplicate"
    assert cl.knob_of(2_013, 1) == "duplicate"
    assert cl.knob_of(3, 0) == "out_of_order"
    assert cl.knob_of(1_503, 1) == "out_of_order"
    assert cl.knob_of(11, 0) == "late"
    assert cl.knob_of(17, 1) == "malformed"
    assert cl.knob_of(29, 0) == "null_referrer"
    assert cl.knob_of(0, 0) is None
    assert cl.knob_of(1, 1) is None


def test_the_canary_falls_only_on_worker_zero_event_1000_and_is_well_formed() -> None:
    hits = [
        (worker, n)
        for worker in WORKERS
        for n in range(1_000_000)
        if cl.knob_of(n, worker) == "canary"
    ]
    assert hits == [(0, 1_000)]
    token = secrets.token_urlsafe(24)
    rng = random.Random(1)
    made = cl.records_for(rng, 7, 0, 1_000, knobs=True, canary=token)
    assert len(made) == 1
    assert made[0].sent.canary is True
    assert made[0].sent.malformed is False
    assert made[0].event["tag"] == token


def test_planned_counts_over_100000_events_of_one_worker_follow_the_residues() -> None:
    counts: dict[str | None, int] = {}
    for n in range(HUNDRED_THOUSAND):
        kind = cl.knob_of(n, 0)
        counts[kind] = counts.get(kind, 0) + 1
    assert counts["duplicate"] == 50
    assert counts["out_of_order"] == 67
    assert counts["late"] == 20
    assert counts["malformed"] == 10
    assert counts["null_referrer"] == 34
    assert counts["canary"] == 1


# --- events -------------------------------------------------------------------------------------


def test_a_malformed_payload_never_starts_with_the_avro_magic_byte() -> None:
    for n in (17, 10_017, 20_017, 999_999):
        payload = cl.malformed_value(n)
        assert payload[0] != 0
        assert payload.startswith(b"\x7f")
        assert str(n).encode() in payload


def test_a_well_formed_payload_starts_with_the_magic_byte_and_the_schema_id() -> None:
    fastavro = pytest.importorskip("fastavro")
    parsed = fastavro.parse_schema(cl.CLICKSTREAM_SCHEMA)
    event = cl.make_event(random.Random(1), 7, 0, 0)
    payload = cl.encode(15, parsed, event)
    assert payload[0] == 0
    assert payload[1:5] == (15).to_bytes(4, "big")


def test_no_generated_referrer_is_the_schema_default_and_product_and_customer_one_are_a_quarter() -> (
    None
):
    rng = random.Random(3)
    events = [cl.make_event(rng, 7, 0, n, knobs=True, canary="x" * 24) for n in range(8_000)]
    referrers = {e["referrer"] for e in events}
    assert "direct" not in referrers
    assert referrers - {None} <= set(cl.REFERRERS)
    assert sum(1 for e in events if e["product_id"] == cl.HOT_PRODUCT_ID) == 2_000
    assert sum(1 for e in events if e["customer_id"] == cl.HOT_CUSTOMER_ID) == 2_000
    assert min(e["product_id"] for e in events if e["product_id"] != 1) >= 2
    assert min(e["customer_id"] for e in events if e["customer_id"] != 1) >= 2
    assert sum(1 for e in events if e["referrer"] is None) == len(
        [n for n in range(8_000) if cl.knob_of(n, 0) == "null_referrer"]
    )


def test_without_knobs_the_events_are_the_earlier_plans_events() -> None:
    rng, reference = random.Random(5 * 1000), random.Random(5 * 1000)
    for n in range(300):
        made = cl.make_event(rng, 5, 0, n)
        product_id = reference.randint(1, cl.PRODUCTS)
        expected = {
            "event_id": f"5-0-{n}",
            "session_id": f"s0-{n // cl.SESSION_SIZE}",
            "customer_id": reference.randint(1, cl.CUSTOMERS),
            "product_id": product_id,
            "event_type": cl.EVENT_TYPES[reference.randrange(len(cl.EVENT_TYPES))],
            "event_time": cl.EVENT_EPOCH_MS + n * cl.EVENT_STEP_MS,
            "page": f"/p/{product_id}",
            "user_agent": cl.USER_AGENTS[reference.randrange(len(cl.USER_AGENTS))],
            "referrer": cl.REFERRERS[reference.randrange(len(cl.REFERRERS))],
            "tag": None,
        }
        assert made == expected


# --- records: what each knob does to the records sent ---------------------------------------------


def test_a_duplicate_is_sent_twice_with_the_same_event_id_and_event_time() -> None:
    made = cl.records_for(random.Random(1), 7, 0, 13, knobs=True, canary=None)
    assert len(made) == 2
    assert made[0].event == made[1].event
    assert made[0].sent.resend is False
    assert made[1].sent.resend is True
    assert made[0].sent.event_time_ms == made[1].sent.event_time_ms


def test_out_of_order_and_late_events_are_shifted_back_by_their_constants() -> None:
    base = cl.EVENT_EPOCH_MS
    step = cl.EVENT_STEP_MS
    early = cl.records_for(random.Random(1), 7, 0, 1_503, knobs=True, canary=None)[0]
    late = cl.records_for(random.Random(1), 7, 0, 5_011, knobs=True, canary=None)[0]
    assert early.event["event_time"] == base + 1_503 * step - cl.OUT_OF_ORDER_SHIFT_MS
    assert late.event["event_time"] == base + 5_011 * step - cl.LATE_SHIFT_MS
    assert cl.LATE_SHIFT_MS > tc.LATENESS_MS > cl.OUT_OF_ORDER_SHIFT_MS


def test_a_malformed_record_keeps_a_valid_key_and_is_flagged() -> None:
    made = cl.records_for(random.Random(1), 7, 1, 17, knobs=True, canary=None)
    assert len(made) == 1
    assert made[0].sent.malformed is True
    assert made[0].event["event_id"] == "7-1-17"


def test_an_event_with_no_knob_is_one_plain_record() -> None:
    made = cl.records_for(random.Random(1), 7, 0, 4, knobs=True, canary=None)
    assert len(made) == 1
    assert made[0].sent == cl.Sent(event_time_ms=cl.EVENT_EPOCH_MS + 4 * cl.EVENT_STEP_MS)


# --- the knob report ----------------------------------------------------------------------------


def run_ledger(token: str, count: int = HUNDRED_THOUSAND) -> dict[str, Any]:
    partitions = [0, 2, 4]
    ledger = cl.KnobLedger(partitions)
    rng = random.Random(7 * 1000)
    next_offset = {p: 100 for p in partitions}
    for n in range(count):
        for record in cl.records_for(rng, 7, 0, n, knobs=True, canary=token):
            partition = partitions[n % len(partitions)]
            ledger.delivered(partition, next_offset[partition], record.sent)
            next_offset[partition] += 1
    return ledger.summary(tc.LATENESS_MS)


def test_the_report_holds_the_counts_the_order_rule_expects() -> None:
    token = secrets.token_urlsafe(24)
    knobs = run_ledger(token)
    assert knobs["intended_duplicates"] == 50
    assert knobs["malformed"]["count"] == 10
    assert knobs["out_of_order_expected"] == 87
    assert knobs["beyond_watermark_expected"] == 20
    assert knobs["explicit_null_referrers"] == 34
    assert knobs["canary_events"] == 1
    assert knobs["hot_product_rows_expected"] == 25_040
    assert knobs["hot_customer_rows_expected"] == 25_000


def test_the_report_gives_malformed_offsets_as_per_partition_runs() -> None:
    knobs = run_ledger(secrets.token_urlsafe(24))
    runs = knobs["malformed"]["runs"]
    assert sum(stop - start for pieces in runs.values() for start, stop in pieces) == 10
    assert set(runs) <= {"0", "2", "4"}
    assert all(isinstance(pieces[0][0], int) for pieces in runs.values())


def test_the_report_never_carries_the_canary_token() -> None:
    token = secrets.token_urlsafe(24)
    knobs = run_ledger(token, count=2_000)
    merged = cl.merge_knob_reports([knobs])
    assert merged["canary_events"] == 1
    assert token not in json.dumps(knobs)
    assert token not in json.dumps(merged)


def test_merged_workers_sum_their_counts_and_state_the_constants() -> None:
    one = run_ledger(secrets.token_urlsafe(24), count=20_000)
    merged = cl.merge_knob_reports([one, one])
    assert merged["intended_duplicates"] == 2 * one["intended_duplicates"]
    assert merged["out_of_order_expected"] == 2 * one["out_of_order_expected"]
    assert merged["lateness_ms"] == tc.LATENESS_MS
    assert merged["configured_hot_share"] == cl.CONFIGURED_HOT_SHARE == 0.2
    assert merged["hot_product_id"] == cl.HOT_PRODUCT_ID
    assert merged["hot_customer_id"] == cl.HOT_CUSTOMER_ID
    assert merged["rates"]["duplicate"] == {"every": 2_000, "residue": 13}
    assert merged["rates"]["canary"] == {"at": 1_000, "worker": 0}


def test_the_report_expects_what_the_order_rule_counts_in_each_partition() -> None:
    ledger = cl.KnobLedger([0])
    plain = cl.Sent(event_time_ms=10_000_000)
    ledger.delivered(0, 0, plain)
    ledger.delivered(0, 1, cl.Sent(event_time_ms=10_000_000 + 1))
    ledger.delivered(0, 2, cl.Sent(event_time_ms=10_000_000 - 90_000))
    ledger.delivered(0, 3, cl.Sent(event_time_ms=10_000_000 + 2))
    ledger.delivered(0, 4, cl.Sent(event_time_ms=10_000_000 - 900_000))
    ledger.delivered(0, 5, cl.Sent(event_time_ms=10_000_000 + 3))
    knobs = ledger.summary(600_000)
    assert knobs["out_of_order_expected"] == 2
    assert knobs["beyond_watermark_expected"] == 1


# --- usage ----------------------------------------------------------------------------------------


def parse(*argv: str) -> argparse.Namespace:
    return cl.build_parser().parse_args(["run", "--events", "10", *argv])


def test_knobs_without_the_canary_token_in_the_environment_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(cl.CANARY_ENV, raising=False)
    assert cl.usage_error(parse("--knobs")) is not None
    assert cl.usage_error(parse()) is None
    monkeypatch.setenv(cl.CANARY_ENV, secrets.token_urlsafe(24))
    assert cl.usage_error(parse("--knobs")) is None
