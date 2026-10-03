"""Unit tests for scripts/cdc_check.py: item 5's pure rule and helpers.

No Kafka, PyIceberg or Postgres is touched (CI installs no spike group and no stack runs): the
offset sets, bronze rows, test_decoding lines and the report are hand-built, shaped like the live
`item5` command's JSON line.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import cdc_check as cc
import connect_admin
import pytest

ORDERS = "shopstream.public.orders"
CUSTOMERS = "shopstream.public.customers"
IN_WINDOW_KILL = {"stage": "ready", "classified": "data_complete", "recovered": True}
OUT_OF_WINDOW_KILL = {"stage": "completed", "classified": "commit_complete", "recovered": True}


def offsets(topic: str, partition: int, count: int, start: int = 0) -> set[cc.Offset]:
    return {(topic, partition, start + n) for n in range(count)}


def make_counts(**changes: Any) -> dict[str, Any]:
    """Counts of a clean run: 100 records, 100 distinct changes, 7 deletes, 12 re-sends."""
    counts: dict[str, Any] = {
        "kafka_nonnull": 100,
        "missing": 0,
        "missing_allowed": 0,
        "extra": 0,
        "duplicate_offsets": 0,
        "bronze_rows": 112,
        "distinct_changes": 100,
        "resends": 12,
        "pg_changes": 100,
        "delete_ok": True,
    }
    counts.update(changes)
    return counts


def make_report(**changes: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "counts": make_counts(),
        "kills": [dict(IN_WINDOW_KILL)],
        "unreadable_tables": [],
    }
    report.update(changes)
    return report


# --- compare_offsets --------------------------------------------------------------------------


def test_equal_sets_below_the_end_offsets_have_nothing_missing_or_extra() -> None:
    kafka = offsets(ORDERS, 0, 5) | offsets(ORDERS, 1, 3)
    result = cc.compare_offsets(kafka, sorted(kafka), {(ORDERS, 0): 5, (ORDERS, 1): 3})
    assert (result["missing"], result["extra"], result["duplicate_offsets"]) == (0, 0, 0)
    assert result["kafka_nonnull"] == 8
    assert result["bronze_in_range"] == 8


def test_a_row_at_or_beyond_the_exclusive_end_offset_is_beyond_end_not_extra() -> None:
    kafka = offsets(ORDERS, 0, 5)
    bronze = [*sorted(kafka), (ORDERS, 0, 5), (ORDERS, 0, 9)]
    result = cc.compare_offsets(kafka, bronze, {(ORDERS, 0): 5})
    assert result["beyond_end"] == 2
    assert result["extra"] == 0
    assert result["missing"] == 0


def test_a_partition_with_no_end_offset_has_only_beyond_rows() -> None:
    result = cc.compare_offsets(set(), [(ORDERS, 2, 0)], {})
    assert (result["beyond_end"], result["extra"]) == (1, 0)


def test_an_offset_bronze_holds_that_kafka_does_not_is_extra() -> None:
    kafka = offsets(ORDERS, 0, 3)
    bronze = [*sorted(kafka), (ORDERS, 0, 3)]
    result = cc.compare_offsets(kafka, bronze, {(ORDERS, 0): 10})
    assert result["extra"] == 1
    assert result["extra_sample"] == [[ORDERS, 0, 3]]


def test_a_repeated_offset_is_one_duplicate_however_often_it_repeats() -> None:
    kafka = offsets(ORDERS, 0, 3)
    bronze = [*sorted(kafka), (ORDERS, 0, 1), (ORDERS, 0, 1)]
    result = cc.compare_offsets(kafka, bronze, {(ORDERS, 0): 3})
    assert result["duplicate_offsets"] == 1
    assert result["duplicate_sample"] == [[ORDERS, 0, 1]]
    assert result["extra"] == 0


def test_the_same_offset_in_two_topics_or_partitions_is_not_a_duplicate() -> None:
    bronze = [(ORDERS, 0, 1), (ORDERS, 1, 1), (CUSTOMERS, 0, 1)]
    kafka = set(bronze)
    ends = {(ORDERS, 0): 2, (ORDERS, 1): 2, (CUSTOMERS, 0): 2}
    assert cc.compare_offsets(kafka, bronze, ends)["duplicate_offsets"] == 0


def test_an_allowed_missing_offset_is_counted_as_allowed_not_hidden() -> None:
    kafka = offsets(ORDERS, 0, 4)
    erased = (ORDERS, 0, 2)
    bronze = sorted(kafka - {erased})
    result = cc.compare_offsets(kafka, bronze, {(ORDERS, 0): 4}, frozenset({erased}))
    assert (result["missing"], result["missing_allowed"]) == (1, 1)
    assert cc.compare_offsets(kafka, bronze, {(ORDERS, 0): 4})["missing_allowed"] == 0


def test_samples_are_sorted_and_cut_to_twenty_beside_the_full_counts() -> None:
    kafka = offsets(ORDERS, 1, 30) | offsets(ORDERS, 0, 30)
    result = cc.compare_offsets(kafka, [], {(ORDERS, 0): 30, (ORDERS, 1): 30})
    assert result["missing"] == 60
    assert len(result["missing_sample"]) == cc.SAMPLE_LIMIT
    assert result["missing_sample"] == sorted(result["missing_sample"])
    assert result["missing_sample"][0] == [ORDERS, 0, 0]


def test_comparing_twice_on_the_same_inputs_gives_the_same_result() -> None:
    kafka = offsets(ORDERS, 0, 6)
    bronze = [*sorted(kafka - {(ORDERS, 0, 4)}), (ORDERS, 0, 1)]
    ends = {(ORDERS, 0): 6}
    assert cc.compare_offsets(kafka, bronze, ends) == cc.compare_offsets(kafka, bronze, ends)


def test_offsets_are_compared_as_ints_not_text() -> None:
    big = 2**53 + 1
    kafka = {(ORDERS, 0, big)}
    assert cc.compare_offsets(kafka, [(ORDERS, 0, big)], {(ORDERS, 0): big + 1})["missing"] == 0
    assert cc.compare_offsets(kafka, [(ORDERS, 0, big - 1)], {(ORDERS, 0): big + 1})["missing"] == 1


def test_compare_offsets_keys_bronze_rows_by_exactly_the_first_three_kafka_metadata_columns() -> (
    None
):
    assert cc.KAFKA_META_COLUMNS[:3] == (
        "_kafka_metadata_topic",
        "_kafka_metadata_partition",
        "_kafka_metadata_offset",
    )
    row = {
        "_kafka_metadata_topic": ORDERS,
        "_kafka_metadata_partition": "1",
        "_kafka_metadata_offset": 7,
        "_kafka_metadata_timestamp": 99,
    }
    assert cc.offset_key(row) == (ORDERS, 1, 7)
    result = cc.compare_offsets({(ORDERS, 1, 7)}, [cc.offset_key(row)], {(ORDERS, 1): 8})
    assert (result["missing"], result["extra"]) == (0, 0)


def test_kafka_meta_columns_name_the_sinks_metadata_columns() -> None:
    assert cc.KAFKA_META_COLUMNS == (
        "_kafka_metadata_topic",
        "_kafka_metadata_partition",
        "_kafka_metadata_offset",
        "_kafka_metadata_timestamp",
    )
    sink = connect_admin.sink_config()
    assert sink["iceberg.tables.route-field"] == cc.KAFKA_META_COLUMNS[0]


# --- change_stats, pk_of and the deletes ------------------------------------------------------


def test_a_resent_change_at_a_new_offset_raises_resends_not_duplicates() -> None:
    change = ("orders", (7,), 5000)
    rows = [change, ("orders", (8,), 5001), change]
    stats = cc.change_stats(rows)
    assert stats == {"bronze_rows": 3, "distinct_changes": 2, "resends": 1}
    kafka = {(ORDERS, 0, 0), (ORDERS, 0, 1), (ORDERS, 0, 2)}
    compared = cc.compare_offsets(kafka, sorted(kafka), {(ORDERS, 0): 3})
    assert compared["duplicate_offsets"] == 0


def test_the_same_key_at_two_lsns_is_two_changes() -> None:
    assert cc.change_stats([("orders", (7,), 1), ("orders", (7,), 2)])["distinct_changes"] == 2


def test_pk_of_uses_after_then_before_as_ints() -> None:
    assert cc.pk_of("customers", {"customer_id": 12, "email": "x"}, None) == (12,)
    assert cc.pk_of("customers", None, {"customer_id": 13}) == (13,)
    assert cc.pk_of("customers", None, None) is None
    assert cc.pk_of("order_items", {"order_id": 4, "line_no": 2}, None) == (4, 2)
    assert cc.pk_of("order_items", None, {"order_id": 4, "line_no": 3}) == (4, 3)


def test_pk_of_keeps_a_long_exactly() -> None:
    big = 2**62 + 3
    assert cc.pk_of("orders", {"order_id": big}, None) == (big,)


def test_a_delete_with_one_distinct_change_and_no_tombstone_row_is_ok() -> None:
    stats = cc.delete_stats(
        [("orders", (1,), 10), ("orders", (1,), 10)],
        1,
        [(ORDERS, 0, 0), (ORDERS, 0, 1)],
        {(ORDERS, 0, 2)},
    )
    assert stats == {
        "pg_deletes": 1,
        "distinct_deletes": 1,
        "rows_at_tombstones": 0,
        "delete_ok": True,
    }


def test_delete_ok_fails_on_a_bronze_row_at_a_tombstone_offset() -> None:
    stats = cc.delete_stats([("orders", (1,), 10)], 1, [(ORDERS, 0, 2)], {(ORDERS, 0, 2)})
    assert stats["rows_at_tombstones"] == 1
    assert stats["delete_ok"] is False


def test_delete_ok_fails_on_two_distinct_delete_changes_for_one_delete() -> None:
    stats = cc.delete_stats([("orders", (1,), 10), ("orders", (1,), 11)], 1, [], set())
    assert stats["distinct_deletes"] == 2
    assert stats["delete_ok"] is False


def test_delete_ok_fails_when_a_postgres_delete_has_no_delete_change() -> None:
    assert cc.delete_stats([], 2, [], set())["delete_ok"] is False


def make_row(op: str, offset: int, key: int = 5, partition: int = 0) -> dict[str, Any]:
    return {
        "table": "orders",
        "topic": ORDERS,
        "partition": partition,
        "offset": offset,
        "op": op,
        "lsn": 100 + offset,
        "pk": (key,),
    }


def test_delete_example_shows_the_rows_and_the_next_tombstone_offset() -> None:
    rows = [make_row("c", 0), make_row("u", 1), make_row("d", 2)]
    example = cc.delete_example(rows, {(ORDERS, 0, 3)})
    assert example is not None
    assert example["key"] == [5]
    assert [r["op"] for r in example["rows"]] == ["c", "u", "d"]
    assert example["delete_offset"] == [0, 2]
    assert example["tombstone_offset"] == 3
    assert example["tombstone_adjacent"] is True
    assert example["tombstone_has_row"] is False


def test_delete_example_skips_a_delete_that_was_resent_and_finds_none_without_deletes() -> None:
    resent = [make_row("d", 2), make_row("d", 7)]
    assert cc.delete_example(resent, {(ORDERS, 0, 3)}) is None
    assert cc.delete_example([make_row("c", 0)], set()) is None


# --- count_changes ----------------------------------------------------------------------------

TD_SAMPLE = [
    "BEGIN",
    "table public.customers: INSERT: customer_id[bigint]:1 email[text]:'a@example.test'",
    "table public.customers: UPDATE: customer_id[bigint]:1 email[text]:'b@example.test'",
    "table public.orders: DELETE: order_id[bigint]:9 status[text]:'new'",
    "table public.order_items: INSERT: order_id[bigint]:9 line_no[integer]:1",
    "table public.reviews: INSERT: review_id[bigint]:4",
    "table public.lakekeeper_tokens: INSERT: id[uuid]:'x'",
    "table public.customers: TRUNCATE: (no-flags)",
    "COMMIT",
]


def test_count_changes_counts_only_the_five_tables_and_three_verbs() -> None:
    counts = cc.count_changes(TD_SAMPLE)
    assert counts["total"] == 5
    assert counts["deletes"] == 1
    assert counts["by_table_op"] == {
        "customers:INSERT": 1,
        "customers:UPDATE": 1,
        "order_items:INSERT": 1,
        "orders:DELETE": 1,
        "reviews:INSERT": 1,
    }


def test_count_changes_of_nothing_is_zero() -> None:
    assert cc.count_changes([]) == {"total": 0, "deletes": 0, "by_table_op": {}}


# --- item5_verdict ----------------------------------------------------------------------------


def test_a_clean_run_with_an_in_window_kill_is_go() -> None:
    verdict = cc.item5_verdict(make_report())
    assert verdict["verdict"] == "go"
    assert verdict["fallback"] is None
    assert verdict["reasons"]


def test_count_star_above_the_distinct_changes_is_still_go() -> None:
    report = make_report(counts=make_counts(bronze_rows=5000, resends=4900))
    assert cc.item5_verdict(report)["verdict"] == "go"


def test_an_allowed_missing_offset_is_still_go() -> None:
    report = make_report(counts=make_counts(missing=2, missing_allowed=2))
    assert cc.item5_verdict(report)["verdict"] == "go"


def test_a_lost_change_is_the_lost_change_fallback() -> None:
    verdict = cc.item5_verdict(make_report(counts=make_counts(distinct_changes=99)))
    assert verdict["verdict"] == "fallback"
    assert verdict["fallback"] == "lost change"
    assert "99" in verdict["reasons"][0]


def test_an_unexplained_missing_offset_is_the_offset_gap_fallback() -> None:
    verdict = cc.item5_verdict(make_report(counts=make_counts(missing=3, missing_allowed=2)))
    assert verdict["verdict"] == "fallback"
    assert verdict["fallback"] == "offset gap"


def test_a_lost_change_and_a_gap_report_both_with_the_lost_change_first() -> None:
    counts = make_counts(distinct_changes=90, missing=1)
    verdict = cc.item5_verdict(make_report(counts=counts))
    assert verdict["fallback"] == "lost change"
    assert len(verdict["reasons"]) == 2


def test_no_kill_at_all_is_inconclusive_and_says_so() -> None:
    verdict = cc.item5_verdict(make_report(kills=[]))
    assert verdict["verdict"] == "inconclusive"
    assert any("kill" in reason for reason in verdict["reasons"])
    assert verdict["fallback"] is None


def test_a_report_without_a_kills_key_has_no_kill() -> None:
    report = make_report()
    del report["kills"]
    assert cc.item5_verdict(report)["verdict"] == "inconclusive"


def test_only_out_of_window_kills_are_inconclusive() -> None:
    kills = [OUT_OF_WINDOW_KILL, {"stage": "initiated", "classified": "workers_writing"}]
    verdict = cc.item5_verdict(make_report(kills=kills))
    assert verdict["verdict"] == "inconclusive"
    assert "2 kill(s)" in verdict["reasons"][0]


def test_a_commit_to_table_kill_is_in_the_window() -> None:
    kills = [{"stage": "ready", "classified": "commit_to_table"}]
    assert cc.item5_verdict(make_report(kills=kills))["verdict"] == "go"


def test_an_empty_kafka_set_is_inconclusive_never_go() -> None:
    report = make_report(counts=make_counts(kafka_nonnull=0, pg_changes=0, distinct_changes=0))
    verdict = cc.item5_verdict(report)
    assert verdict["verdict"] == "inconclusive"
    assert any("Kafka set is empty" in reason for reason in verdict["reasons"])


def test_an_unreadable_table_is_inconclusive_and_named() -> None:
    verdict = cc.item5_verdict(make_report(unreadable_tables=["orders"]))
    assert verdict["verdict"] == "inconclusive"
    assert any("orders" in reason for reason in verdict["reasons"])


def test_a_missing_count_is_inconclusive_and_named() -> None:
    counts = make_counts()
    del counts["distinct_changes"]
    verdict = cc.item5_verdict(make_report(counts=counts))
    assert verdict["verdict"] == "inconclusive"
    assert "distinct_changes" in verdict["reasons"][0]


def test_a_report_without_counts_is_inconclusive() -> None:
    assert cc.item5_verdict({"kills": [IN_WINDOW_KILL]})["verdict"] == "inconclusive"


def test_a_duplicate_offset_with_no_loss_is_inconclusive_with_that_reason() -> None:
    verdict = cc.item5_verdict(make_report(counts=make_counts(duplicate_offsets=2)))
    assert verdict["verdict"] == "inconclusive"
    assert verdict["fallback"] is None
    assert "appear more than once" in verdict["reasons"][0]


def test_an_extra_offset_or_a_failed_delete_check_is_inconclusive() -> None:
    assert cc.item5_verdict(make_report(counts=make_counts(extra=1)))["verdict"] == "inconclusive"
    failed = make_counts(delete_ok=False)
    assert cc.item5_verdict(make_report(counts=failed))["verdict"] == "inconclusive"


def test_a_lost_change_without_a_kill_is_inconclusive_but_still_shows_the_loss() -> None:
    report = make_report(kills=[], counts=make_counts(distinct_changes=90))
    verdict = cc.item5_verdict(report)
    assert verdict["verdict"] == "inconclusive"
    assert any("lost" in reason for reason in verdict["reasons"])


def test_the_verdict_does_not_change_its_input() -> None:
    report = make_report()
    before = copy.deepcopy(report)
    cc.item5_verdict(report)
    assert report == before


def test_read_kills_keeps_json_objects_and_skips_everything_else() -> None:
    lines = [
        '{"stage": "ready"}\n',
        "\n",
        "note: not json\n",
        '{"broken": \n',
        '{"stage": "initiated"}',
    ]
    assert [k["stage"] for k in cc.read_kills(lines)] == ["ready", "initiated"]


def test_the_in_window_stages_are_the_two_between_data_complete_and_commit_complete() -> None:
    assert frozenset({"data_complete", "commit_to_table"}) == cc.IN_WINDOW


def test_the_topic_names_come_from_connect_admin() -> None:
    assert [connect_admin.topic(table) for table in cc.PK_COLUMNS] == [
        f"shopstream.public.{table}" for table in cc.PK_COLUMNS
    ]
    assert set(cc.PK_COLUMNS) == set(connect_admin.TABLES)


# --- control topic: decode, classify, kill window ---------------------------------------------

COMMIT_A = "0f8e7c2a-1b2c-4d3e-8f90-123456789abc"
COMMIT_B = "7a1b2c3d-4e5f-4a6b-8c7d-0123456789ef"


def zigzag(value: int) -> bytes:
    """Avro's zigzag varint, the encoding of a long."""
    number = (value << 1) ^ (value >> 63)
    out = bytearray()
    while number > 0x7F:
        out.append((number & 0x7F) | 0x80)
        number >>= 7
    out.append(number)
    return bytes(out)


def control_bytes(
    type_id: int,
    commit_id: str,
    timestamp_us: int = 1_790_000_000_123_456,
    group: str = "cg-control-x",
) -> bytes:
    """A control event as the Iceberg sink writes it: magic, writeUTF schema, id, type, ts, group, commit id."""
    schema = b'{"type":"record","name":"Event"}'
    head = b"\xc2\x01" + len(schema).to_bytes(2, "big") + schema
    body = bytes(range(16)) + zigzag(type_id) + zigzag(timestamp_us)
    body += zigzag(len(group)) + group.encode()
    return head + body + uuid.UUID(commit_id).bytes


def event(type_name: str, commit_id: str, timestamp_us: int = 1) -> dict[str, Any]:
    return {"type": type_name, "commit_id": commit_id, "timestamp_us": timestamp_us, "group": "g"}


def test_decode_control_head_reads_type_timestamp_group_and_commit_id() -> None:
    head = cc.decode_control_head(control_bytes(2, COMMIT_A))
    assert head == {
        "type": "DATA_COMPLETE",
        "timestamp_us": 1_790_000_000_123_456,
        "group": "cg-control-x",
        "commit_id": COMMIT_A,
    }


@pytest.mark.parametrize(("type_id", "name"), sorted(cc.CONTROL_TYPES.items()))
def test_every_control_type_decodes_by_its_number(type_id: int, name: str) -> None:
    assert cc.decode_control_head(control_bytes(type_id, COMMIT_B))["type"] == name


def test_the_five_control_types_are_the_iceberg_payload_types() -> None:
    assert cc.CONTROL_TYPES == {
        0: "START_COMMIT",
        1: "DATA_WRITTEN",
        2: "DATA_COMPLETE",
        3: "COMMIT_TO_TABLE",
        4: "COMMIT_COMPLETE",
    }


def test_bytes_without_the_magic_are_a_value_error() -> None:
    with pytest.raises(ValueError, match="magic"):
        cc.decode_control_head(b"\x00\x01" + control_bytes(2, COMMIT_A)[2:])


def test_a_truncated_event_is_a_value_error_not_an_index_error() -> None:
    with pytest.raises(ValueError, match="short"):
        cc.decode_control_head(control_bytes(2, COMMIT_A)[:-5])


def test_an_unknown_control_type_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="type"):
        cc.decode_control_head(control_bytes(9, COMMIT_A))


@pytest.mark.parametrize("value", [0, 1, -1, 63, 64, -65, 2**40, 1_790_000_000_123_456, -(2**62)])
def test_zigzag_long_round_trips_a_long_and_returns_the_next_index(value: int) -> None:
    data = b"\xff" + zigzag(value) + b"\xee"
    assert cc.zigzag_long(data, 1) == (value, len(data) - 1)


def test_a_commit_with_only_start_commit_is_workers_writing() -> None:
    events = [event("START_COMMIT", COMMIT_A)]
    assert cc.classify_kill(COMMIT_A, events) == "workers_writing"
    events.append(event("DATA_WRITTEN", COMMIT_A))
    assert cc.classify_kill(COMMIT_A, events) == "workers_writing"


def test_data_complete_without_commit_to_table_is_data_complete() -> None:
    events = [event("START_COMMIT", COMMIT_A), event("DATA_COMPLETE", COMMIT_A)]
    assert cc.classify_kill(COMMIT_A, events) == "data_complete"


def test_commit_to_table_without_commit_complete_is_commit_to_table() -> None:
    events = [
        event(name, COMMIT_A) for name in ("START_COMMIT", "DATA_COMPLETE", "COMMIT_TO_TABLE")
    ]
    assert cc.classify_kill(COMMIT_A, events) == "commit_to_table"


def test_commit_complete_is_commit_complete_and_outside_the_window() -> None:
    names = ("START_COMMIT", "DATA_COMPLETE", "COMMIT_TO_TABLE", "COMMIT_COMPLETE")
    kind = cc.classify_kill(COMMIT_A, [event(name, COMMIT_A) for name in names])
    assert kind == "commit_complete"
    assert kind not in cc.IN_WINDOW


def test_another_commits_events_do_not_classify_this_kill() -> None:
    events = [event("COMMIT_COMPLETE", COMMIT_B), event("START_COMMIT", COMMIT_A)]
    assert cc.classify_kill(COMMIT_A, events) == "workers_writing"


def test_a_missing_commit_id_or_no_event_is_unknown() -> None:
    assert cc.classify_kill(None, [event("START_COMMIT", COMMIT_A)]) == "unknown"
    assert cc.classify_kill(COMMIT_A, []) == "unknown"
    assert cc.classify_kill(COMMIT_A, [event("START_COMMIT", COMMIT_B)]) == "unknown"


def test_the_kill_window_holds_the_killed_commit_and_the_next_one_in_topic_order() -> None:
    events = [
        event("COMMIT_COMPLETE", "00000000-0000-4000-8000-000000000000", 1),
        event("START_COMMIT", COMMIT_A, 2),
        event("DATA_COMPLETE", COMMIT_A, 3),
        event("START_COMMIT", COMMIT_B, 4),
        event("DATA_COMPLETE", COMMIT_B, 5),
        event("COMMIT_COMPLETE", COMMIT_B, 6),
    ]
    window = cc.kill_window(COMMIT_A, events)
    assert [(w["type"], w["commit"]) for w in window] == [
        ("START_COMMIT", "0f8e7c2a"),
        ("DATA_COMPLETE", "0f8e7c2a"),
        ("START_COMMIT", "7a1b2c3d"),
        ("DATA_COMPLETE", "7a1b2c3d"),
        ("COMMIT_COMPLETE", "7a1b2c3d"),
    ]
    assert [w["timestamp_us"] for w in window] == [2, 3, 4, 5, 6]


def test_the_kill_window_of_a_commit_with_no_next_commit_is_just_its_events() -> None:
    window = cc.kill_window(COMMIT_A, [event("START_COMMIT", COMMIT_A)])
    assert [w["commit"] for w in window] == ["0f8e7c2a"]
    assert cc.kill_window(None, [event("START_COMMIT", COMMIT_A)]) == []
    assert cc.kill_window(COMMIT_A, []) == []


def test_classify_kills_attaches_the_class_and_the_window_without_changing_the_record() -> None:
    kills: list[dict[str, Any]] = [
        {"stage": "ready", "commit_id": COMMIT_A},
        {"stage": "initiated", "commit_id": None},
    ]
    events = [event("START_COMMIT", COMMIT_A), event("DATA_COMPLETE", COMMIT_A)]
    classified = cc.classify_kills(kills, events)
    assert [k["classified"] for k in classified] == ["data_complete", "unknown"]
    assert classified[0]["stage"] == "ready"
    assert len(classified[0]["events"]) == 2
    assert "classified" not in kills[0]


def test_a_kill_classified_data_complete_makes_a_go_report_in_window() -> None:
    kills = cc.classify_kills(
        [{"stage": "ready", "commit_id": COMMIT_A}],
        [event("START_COMMIT", COMMIT_A), event("DATA_COMPLETE", COMMIT_A)],
    )
    assert cc.item5_verdict(make_report(kills=kills))["verdict"] == "go"
    out = cc.classify_kills(
        [{"stage": "completed", "commit_id": COMMIT_A}],
        [event("COMMIT_COMPLETE", COMMIT_A)],
    )
    assert cc.item5_verdict(make_report(kills=out))["verdict"] == "inconclusive"
