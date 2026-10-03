"""Unit tests for scripts/throughput_check.py: item 12's commit reader, throughput rule and offset runs.

No Docker, Kafka or Iceberg: the live reads (snapshots, islands, log dirs) are exercised by the
tracer and dry runs of Plan 05-02. What is pinned here is the arithmetic and the ordering that
decide a verdict: each threshold passes at the line and fails one step past it, touching runs merge,
and only an append snapshot that added rows is a commit.
"""

from __future__ import annotations

from typing import Any

import pytest
import throughput_check as tc

T0 = 1_790_000_000_000


def snap(
    snapshot_id: int,
    seq: int,
    at_ms: int,
    rows: int,
    operation: str = "append",
) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "sequence_number": seq,
        "timestamp_ms": at_ms,
        "operation": operation,
        "added_records": rows,
        "total_records": rows,
    }


def commit(at_ms: int, rows: int) -> dict[str, int]:
    return {"snapshot_id": 1, "sequence_number": 1, "committed_at_ms": at_ms, "added_records": rows}


# --- commits_from_snapshots ---------------------------------------------------------------------


def test_commits_keep_only_append_snapshots_that_added_rows() -> None:
    found = [
        snap(1, 1, T0, 100),
        snap(2, 2, T0 + 60_000, 0),
        snap(3, 3, T0 + 120_000, 50, operation="overwrite"),
        snap(4, 4, T0 + 180_000, 70, operation="Operation.APPEND"),
    ]
    commits = tc.commits_from_snapshots(found)
    assert [c["snapshot_id"] for c in commits] == [1, 4]
    assert [c["added_records"] for c in commits] == [100, 70]
    assert [c["committed_at_ms"] for c in commits] == [T0, T0 + 180_000]


def test_commits_sort_by_sequence_then_time_then_id_whatever_the_listing_order() -> None:
    found = [
        snap(30, 3, T0 + 3, 1),
        snap(11, 1, T0 + 9, 1),
        snap(20, 2, T0 + 1, 1),
        snap(10, 1, T0 + 9, 1),
    ]
    commits = tc.commits_from_snapshots(found)
    assert [c["snapshot_id"] for c in commits] == [10, 11, 20, 30]
    assert tc.commits_from_snapshots(list(reversed(found))) == commits


def test_a_snapshot_with_no_added_records_key_or_a_text_count_is_handled() -> None:
    odd: dict[str, Any] = {"snapshot_id": 5, "sequence_number": 1, "timestamp_ms": T0}
    odd["operation"] = "append"
    text = {**odd, "snapshot_id": 6, "sequence_number": 2, "added_records": "12"}
    assert tc.commits_from_snapshots([odd, text]) == [
        {"snapshot_id": 6, "sequence_number": 2, "committed_at_ms": T0, "added_records": 12}
    ]
    assert tc.commits_from_snapshots([]) == []


# --- committed_throughput -----------------------------------------------------------------------


def test_throughput_is_rows_of_commits_2_to_n_over_the_time_from_commit_1_to_n() -> None:
    commits = [commit(T0, 900_000), commit(T0 + 60_000, 840_000), commit(T0 + 120_000, 840_000)]
    result = tc.committed_throughput(commits)
    assert result["computable"] is True
    assert result["commits"] == 3
    assert result["rows_2_to_n"] == 1_680_000
    assert result["span_ms"] == 120_000
    assert result["events_per_s"] == 14_000.0
    assert result["meets_floor"] is True


def test_the_floor_passes_exactly_at_14000_events_per_second_and_fails_one_row_fewer() -> None:
    at_line = [commit(T0, 1), commit(T0 + 120_000, 1_680_000)]
    one_short = [commit(T0, 1), commit(T0 + 120_000, 1_679_999)]
    assert tc.committed_throughput(at_line)["meets_floor"] is True
    short = tc.committed_throughput(one_short)
    assert short["meets_floor"] is False
    # Displayed to one decimal this reads 14,000.0, yet the verdict (integers) still fails it.
    assert short["events_per_s"] == 14_000.0


def test_the_floor_is_decided_in_integers_not_by_the_rounded_display() -> None:
    # 13,999.96 events/s displays as 14,000.0 but is under the line.
    rows, span_ms = 1_399_996, 100_000
    result = tc.committed_throughput([commit(T0, 1), commit(T0 + span_ms, rows)])
    assert result["events_per_s"] == 14_000.0
    assert result["meets_floor"] is False


def test_the_displayed_rate_rounds_half_even_to_one_decimal() -> None:
    # Exact ties at the second decimal: 0.25 -> 0.2, 0.35 -> 0.4, 0.45 -> 0.4 (rows x 1000 / span_ms).
    for rows, span_ms, shown in ((1, 4_000, 0.2), (7, 20_000, 0.4), (9, 20_000, 0.4)):
        result = tc.committed_throughput([commit(T0, 1), commit(T0 + span_ms, rows)])
        assert result["events_per_s"] == shown


@pytest.mark.parametrize(
    "commits",
    [
        [],
        [commit(T0, 100)],
        [commit(T0, 100), commit(T0, 100)],
        [commit(T0 + 5, 100), commit(T0, 100)],
    ],
)
def test_throughput_is_not_computable_below_two_commits_or_at_a_zero_span(
    commits: list[dict[str, int]],
) -> None:
    result = tc.committed_throughput(commits)
    assert result["computable"] is False
    assert result["meets_floor"] is False
    assert result["events_per_s"] is None


# --- offset_runs --------------------------------------------------------------------------------


def test_runs_that_touch_merge_into_one() -> None:
    assert tc.offset_runs([0, 1, 2, 3, 4, 5, 6, 7, 8]) == ([[0, 9]], 0)
    assert tc.offset_runs([*range(0, 5), *range(5, 9)]) == ([[0, 9]], 0)


def test_a_one_offset_gap_keeps_two_runs() -> None:
    assert tc.offset_runs([*range(0, 4), *range(5, 9)]) == ([[0, 4], [5, 9]], 0)


def test_a_repeated_offset_counts_once_as_a_duplicate_however_often_it_repeats() -> None:
    assert tc.offset_runs([0, 1, 1, 1, 2]) == ([[0, 3]], 1)
    assert tc.offset_runs([0, 0, 1, 1, 2]) == ([[0, 3]], 2)


def test_runs_do_not_depend_on_the_order_the_offsets_arrive_in() -> None:
    shuffled = [7, 2, 9, 3, 8, 0, 1]
    assert tc.offset_runs(shuffled) == ([[0, 4], [7, 10]], 0)


def test_no_offsets_make_no_runs() -> None:
    assert tc.offset_runs([]) == ([], 0)


def test_a_million_contiguous_offsets_are_one_run() -> None:
    assert tc.offset_runs(range(5, 1_000_005)) == ([[5, 1_000_005]], 0)
