"""Unit tests for scripts/bronze_wap.py: the SQL builders and the pure write-audit-publish rules.

No Spark, Kafka or PyIceberg is touched: the live steps need a running stack (ADR-001 Evidence
rules). Ledgers are synthetic integer keys, and nothing here holds a row value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import bronze_wap as bw
import pytest

CUSTOMERS = "shopstream.public.customers"


# --- SQL builders -----------------------------------------------------------------------------


def test_qualified_names_audit_through_the_branch_suffix_and_main_by_the_plain_name() -> None:
    assert bw.qualified("customers") == "rest.bronze.customers"
    assert bw.qualified("customers", "main") == "rest.bronze.customers"
    assert bw.qualified("customers", "audit") == "rest.bronze.customers.branch_audit"


@pytest.mark.parametrize(
    "table", ["customers; DROP TABLE x", "bronze.customers", "", "1x", "users"]
)
def test_a_bad_or_unknown_table_name_is_refused(table: str) -> None:
    with pytest.raises(ValueError, match=r"plain SQL identifier|bronze table"):
        bw.qualified(table)


def test_an_unknown_branch_is_refused_everywhere() -> None:
    with pytest.raises(ValueError, match="branch"):
        bw.qualified("customers", "dev")
    with pytest.raises(ValueError, match="branch"):
        bw.rewrite_sql("customers", "x'; --")
    with pytest.raises(ValueError, match="branch"):
        bw.count_keys_sql("customers", [1], "x'; --")


def test_pk_predicate_reads_the_key_from_after_then_before_with_sorted_unique_ints() -> None:
    assert bw.pk_predicate("customers", [9, 3, 3, 5]) == (
        "coalesce(after.customer_id, before.customer_id) IN (3, 5, 9)"
    )
    assert bw.pk_predicate("products", [1]) == (
        "coalesce(after.product_id, before.product_id) IN (1)"
    )


@pytest.mark.parametrize("keys", [[], ["5"], [5.0], [True], [None], [1, "x"]])
def test_pk_predicate_refuses_an_empty_list_or_a_non_integer(keys: list[object]) -> None:
    with pytest.raises(ValueError, match="ledger"):
        bw.pk_predicate("customers", keys)  # type: ignore[arg-type]


def test_pk_predicate_refuses_a_multi_column_primary_key() -> None:
    with pytest.raises(ValueError, match="composite"):
        bw.pk_predicate("order_items", [1])


def test_erase_sql_on_audit_and_on_main() -> None:
    assert bw.erase_sql("customers", [2, 7], "audit") == (
        "DELETE FROM rest.bronze.customers.branch_audit "
        "WHERE coalesce(after.customer_id, before.customer_id) IN (2, 7)"
    )
    assert bw.erase_sql("customers", [2, 7], "main") == (
        "DELETE FROM rest.bronze.customers "
        "WHERE coalesce(after.customer_id, before.customer_id) IN (2, 7)"
    )


def test_rewrite_sql_names_the_branch_and_rewrites_every_group_of_two_or_more_files() -> None:
    assert bw.rewrite_sql("customers", "audit") == (
        "CALL rest.system.rewrite_data_files(table => 'bronze.customers', branch => 'audit', "
        "options => map('rewrite-all', 'true', 'min-input-files', '2'))"
    )
    assert "branch => 'main'" in bw.rewrite_sql("customers", "main")


def test_fast_forward_sql_moves_main_to_audit() -> None:
    assert bw.fast_forward_sql("orders") == (
        "CALL rest.system.fast_forward('bronze.orders', 'main', 'audit')"
    )


def test_expire_sql_renders_the_utc_timestamp_and_keeps_one_snapshot() -> None:
    moment = datetime(2026, 10, 3, 12, 30, 45, 123456, tzinfo=UTC)
    assert bw.expire_sql("customers", moment) == (
        "CALL rest.system.expire_snapshots(table => 'bronze.customers', "
        "older_than => TIMESTAMP '2026-10-03 12:30:45.123456', retain_last => 1)"
    )


def test_expire_sql_converts_a_non_utc_aware_datetime_and_refuses_a_naive_one() -> None:
    plus_seven = timezone(timedelta(hours=7))
    moment = datetime(2026, 10, 3, 19, 30, 45, 5, tzinfo=plus_seven)
    assert "TIMESTAMP '2026-10-03 12:30:45.000005'" in bw.expire_sql("customers", moment)
    with pytest.raises(ValueError, match="timezone-aware"):
        bw.expire_sql("customers", datetime(2026, 10, 3, 12, 30, 45))


def test_replace_branch_sql_and_rollback_sql_take_an_integer_snapshot_id() -> None:
    assert bw.replace_branch_sql("customers", 8123456789012345678) == (
        "ALTER TABLE rest.bronze.customers CREATE OR REPLACE BRANCH audit "
        "AS OF VERSION 8123456789012345678"
    )
    assert bw.rollback_sql("customers", 42) == (
        "CALL rest.system.rollback_to_snapshot('bronze.customers', 42)"
    )
    for builder in (bw.replace_branch_sql, bw.rollback_sql):
        with pytest.raises(ValueError, match="integer"):
            builder("customers", "1; DROP")  # type: ignore[arg-type]


def test_count_keys_sql_reads_a_branch_by_name_and_a_snapshot_by_id() -> None:
    assert bw.count_keys_sql("customers", [4], "audit") == (
        "SELECT count(*) FROM rest.bronze.customers VERSION AS OF 'audit' "
        "WHERE coalesce(after.customer_id, before.customer_id) IN (4)"
    )
    assert bw.count_keys_sql("customers", [4], 77) == (
        "SELECT count(*) FROM rest.bronze.customers VERSION AS OF 77 "
        "WHERE coalesce(after.customer_id, before.customer_id) IN (4)"
    )


# --- pick_ledger ------------------------------------------------------------------------------


def test_pick_ledger_takes_each_partitions_highest_offset_key_and_the_two_oldest() -> None:
    rows = [
        (0, 0, 11),
        (0, 5, 12),
        (0, 9, 13),
        (1, 1, 21),
        (1, 8, 22),
        (2, 2, 31),
        (2, 3, 32),
        (2, 7, 33),
    ]
    # highest per partition: 13, 22, 33; the two lowest offsets not chosen: 11 (0) and 21 (1)
    assert bw.pick_ledger(rows) == [11, 13, 21, 22, 33]


def test_pick_ledger_has_no_duplicates_and_skips_keys_already_chosen() -> None:
    rows = [(0, 0, 5), (0, 1, 5), (0, 2, 6), (1, 3, 7)]
    ledger = bw.pick_ledger(rows)
    assert ledger == sorted(set(ledger))
    assert 6 in ledger
    assert 7 in ledger


def test_pick_ledger_on_few_rows_returns_what_exists_and_ints_only() -> None:
    assert bw.pick_ledger([(0, 4, 9)]) == [9]
    assert bw.pick_ledger([]) == []
    assert all(isinstance(key, int) for key in bw.pick_ledger([(0, 1, 2), (1, 2, 3)]))


# --- resume_offsets ---------------------------------------------------------------------------


def test_resume_offsets_is_one_past_the_highest_row_and_log_start_for_no_rows() -> None:
    result = bw.resume_offsets(
        {("t", 0): 41, ("t", 2): 7},
        [("t", 0), ("t", 1), ("t", 2)],
        {("t", 0): 0, ("t", 1): 3, ("t", 2): 0},
    )
    assert result == {("t", 0): 42, ("t", 1): 3, ("t", 2): 8}


def test_resume_offsets_with_no_rows_resumes_every_partition_at_log_start() -> None:
    partitions = [("t", 0), ("t", 1)]
    assert bw.resume_offsets({}, partitions, {("t", 0): 5, ("t", 1): 0}) == {
        ("t", 0): 5,
        ("t", 1): 0,
    }


def test_resume_offsets_refuses_a_partition_it_was_not_given() -> None:
    with pytest.raises(ValueError, match="not given"):
        bw.resume_offsets({("t", 9): 3}, [("t", 0)], {("t", 0): 0})


def test_resume_offsets_refuses_a_partition_with_neither_rows_nor_log_start() -> None:
    with pytest.raises(ValueError, match="log-start"):
        bw.resume_offsets({}, [("t", 0)], {})


def test_resume_offsets_is_sorted_by_topic_and_partition() -> None:
    result = bw.resume_offsets(
        {}, [("b", 0), ("a", 1), ("a", 0)], {("b", 0): 0, ("a", 1): 0, ("a", 0): 0}
    )
    assert list(result) == [("a", 0), ("a", 1), ("b", 0)]


# --- written_by, lineage and the ordering guard -----------------------------------------------


def test_written_by_is_sink_only_for_a_kafka_connect_summary_key() -> None:
    assert bw.written_by(["kafka.connect.commit-id", "added-data-files"]) == "sink"
    assert bw.written_by(["kafka.connect.offsets.control-iceberg.0"]) == "sink"
    assert bw.written_by(["spark.app.id", "added-data-files"]) == "spark"
    assert bw.written_by([]) == "spark"


def test_lineage_ops_lists_the_operations_after_the_base_oldest_first() -> None:
    snapshots: dict[int, tuple[int | None, str]] = {
        1: (None, "append"),
        2: (1, "append"),
        3: (2, "overwrite"),
        4: (3, "replace"),
    }
    assert bw.lineage_ops(snapshots, 4, 2) == ["overwrite", "replace"]
    assert bw.lineage_ops(snapshots, 4, 4) == []
    assert bw.lineage_ops(snapshots, 2, None) == ["append", "append"]


def test_lineage_ops_refuses_a_base_that_is_not_an_ancestor() -> None:
    snapshots: dict[int, tuple[int | None, str]] = {1: (None, "append"), 2: (None, "append")}
    with pytest.raises(ValueError, match="ancestor"):
        bw.lineage_ops(snapshots, 2, 1)


def test_expire_snapshots_never_runs_before_the_fast_forward() -> None:
    with pytest.raises(RuntimeError, match="after the fast-forward"):
        bw.guard_expire(["stop", "erase", "rewrite"], sink_stopped=True)


def test_expire_snapshots_never_runs_while_the_sink_is_running() -> None:
    with pytest.raises(RuntimeError, match="sink is running"):
        bw.guard_expire(["stop", "erase", "rewrite", "fast_forward"], sink_stopped=False)


def test_expire_snapshots_runs_after_the_fast_forward_with_the_sink_stopped() -> None:
    bw.guard_expire(["stop", "erase", "rewrite", "fast_forward"], sink_stopped=True)


# --- ledger and report handling ---------------------------------------------------------------


def test_ledger_of_accepts_integers_only() -> None:
    assert bw.ledger_of({"ledger": [3, 1, 2]}) == [3, 1, 2]
    bad_reports: list[dict[str, Any]] = [
        {},
        {"ledger": []},
        {"ledger": ["a@b.example"]},
        {"ledger": [1, True]},
    ]
    for bad in bad_reports:
        with pytest.raises(ValueError, match="ledger"):
            bw.ledger_of(bad)


def test_last_json_object_skips_log_noise_and_bad_lines() -> None:
    text = 'WARN something\n{"a": 1}\n{not json\nINFO done\n'
    assert bw.last_json_object(text) == {"a": 1}
    assert bw.last_json_object("no json here\n") is None
    assert bw.last_json_object('[1, 2]\n{"b": 2}\n') == {"b": 2}


def test_the_ledger_table_and_branches_are_fixed() -> None:
    assert bw.LEDGER_TABLE == "customers"
    assert bw.BRANCHES == ("audit", "main")
