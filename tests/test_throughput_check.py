"""Unit tests for scripts/throughput_check.py: item 12's commit reader, throughput rule and offset runs.

No Docker, Kafka or Iceberg: the live reads (snapshots, islands, log dirs) are exercised by the
tracer and dry runs of Plan 05-02. What is pinned here is the arithmetic and the ordering that
decide a verdict: each threshold passes at the line and fails one step past it, touching runs merge,
and only an append snapshot that added rows is a commit.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import re
from pathlib import Path
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


# --- compare_runs -------------------------------------------------------------------------------


def test_equal_runs_have_no_missing_and_no_extra() -> None:
    result = tc.compare_runs({0: [[0, 10]]}, {0: [[0, 10]]}, {})
    assert result["equal"] is True
    assert (result["missing"], result["extra"], result["missing_allowed"]) == (0, 0, 0)
    assert result["missing_sample"] == []
    assert result["extra_sample"] == []


def test_a_one_offset_hole_in_bronze_is_one_missing_with_its_coordinates() -> None:
    result = tc.compare_runs({0: [[0, 10]]}, {0: [[0, 4], [5, 10]]}, {})
    assert result["equal"] is False
    assert result["missing"] == 1
    assert result["missing_sample"] == [[0, 4, 5]]


def test_an_offset_bronze_has_beyond_the_expected_runs_is_extra() -> None:
    result = tc.compare_runs({0: [[0, 10]]}, {0: [[0, 11]]}, {})
    assert result["equal"] is False
    assert (result["missing"], result["extra"]) == (0, 1)
    assert result["extra_sample"] == [[0, 10, 11]]


def test_an_allowed_missing_offset_leaves_the_sets_equal_and_is_counted_beside() -> None:
    result = tc.compare_runs({0: [[0, 10]]}, {0: [[0, 4], [5, 10]]}, {0: [[4, 5]]})
    assert result["equal"] is True
    assert (result["missing"], result["missing_allowed"]) == (0, 1)


def test_an_allowed_offset_that_bronze_holds_anyway_is_not_extra() -> None:
    result = tc.compare_runs({0: [[0, 10]]}, {0: [[0, 10]]}, {0: [[4, 5]]})
    assert result["equal"] is True
    assert (result["missing"], result["extra"], result["missing_allowed"]) == (0, 0, 0)


def test_a_partition_only_in_bronze_is_all_extra_and_one_only_expected_is_all_missing() -> None:
    result = tc.compare_runs({0: [[0, 3]]}, {0: [[0, 3]], 1: [[0, 2]]}, {})
    assert (result["extra"], result["missing"]) == (2, 0)
    assert result["extra_sample"] == [[1, 0, 2]]
    lone = tc.compare_runs({0: [[0, 3]], 2: [[5, 8]]}, {0: [[0, 3]]}, {})
    assert (lone["extra"], lone["missing"]) == (0, 3)
    assert lone["missing_sample"] == [[2, 5, 8]]


def test_runs_from_json_with_text_partition_keys_and_unsorted_touching_pieces_compare_equal() -> (
    None
):
    result = tc.compare_runs({"0": [[5, 9], [0, 5]]}, {0: [[0, 9]]}, {})
    assert result["equal"] is True


def test_samples_are_sorted_and_cut_to_twenty() -> None:
    expected = {0: [[0, 100]]}
    holes = [[i, i + 1] for i in range(0, 100, 2)]  # 50 evens present, odds missing
    result = tc.compare_runs(expected, {0: holes}, {})
    assert result["missing"] == 50
    assert len(result["missing_sample"]) == 20
    assert result["missing_sample"] == sorted(result["missing_sample"])


@pytest.mark.parametrize("bad", [[[3, 3]], [[5, 2]], [["a", "b"]], [[1]], [[-1, 4]]])
def test_an_invalid_run_is_refused(bad: list[list[Any]]) -> None:
    with pytest.raises(ValueError, match="run"):
        tc.compare_runs({0: bad}, {0: []}, {})


# --- islands_from_rows --------------------------------------------------------------------------


def test_islands_from_rows_groups_by_partition_and_sorts() -> None:
    rows = [(1, 0, 4), (0, 5, 9), (0, 0, 4), (1, 4, 6)]
    assert tc.islands_from_rows(rows) == {0: [[0, 4], [5, 9]], 1: [[0, 6]]}


def test_islands_from_rows_refuses_an_empty_or_inverted_island_and_a_non_integer() -> None:
    for bad in [(0, 3, 3), (0, 5, 2), (0, "1", 2), (0.5, 1, 2), (0, 1)]:
        with pytest.raises(ValueError, match="island"):
            tc.islands_from_rows([bad])


# --- parse_log_dirs -----------------------------------------------------------------------------


def log_dirs_text(sizes: dict[int, list[int]], *, other: bool = True) -> str:
    brokers = []
    for broker, per_partition in sizes.items():
        partitions = [
            {"partition": f"clickstream-{i}", "size": size, "offsetLag": 0, "isFuture": False}
            for i, size in enumerate(per_partition)
        ]
        if other:
            partitions.append({"partition": "clickstream.dlq-0", "size": 7, "isFuture": False})
            partitions.append({"partition": "fx.refresh-0", "size": 9, "isFuture": False})
        brokers.append(
            {
                "broker": broker,
                "logDirs": [{"logDir": "/data", "error": None, "partitions": partitions}],
            }
        )
    body = json.dumps({"version": 1, "brokers": brokers})
    return f"Querying brokers for log directories information\nReceived log directory information\n{body}\n"


def test_log_dirs_sum_every_replica_of_the_topic_once_and_ignore_other_topics() -> None:
    sizes = {1: [10, 20, 30, 40, 50, 60], 2: [11, 21, 31, 41, 51, 61], 3: [12, 22, 32, 42, 52, 62]}
    result = tc.parse_log_dirs(log_dirs_text(sizes), "clickstream")
    assert result == {"bytes": sum(sum(v) for v in sizes.values()), "replicas": 18, "brokers": 3}


def test_log_dirs_skip_a_future_replica() -> None:
    body = {
        "version": 1,
        "brokers": [
            {
                "broker": 1,
                "logDirs": [
                    {
                        "partitions": [
                            {"partition": "clickstream-0", "size": 5, "isFuture": False},
                            {"partition": "clickstream-0", "size": 99, "isFuture": True},
                        ]
                    }
                ],
            }
        ],
    }
    result = tc.parse_log_dirs(json.dumps(body), "clickstream")
    assert (result["bytes"], result["replicas"]) == (5, 1)


@pytest.mark.parametrize(
    "text", ["", "Querying brokers\nReceived\n", '{"version": 1}\n', "[1, 2]\n"]
)
def test_log_dirs_without_a_broker_json_line_raise(text: str) -> None:
    with pytest.raises(ValueError, match="log"):
        tc.parse_log_dirs(text, "clickstream")


# --- parse_df and parse_colima_list -------------------------------------------------------------


def test_parse_df_reads_the_three_integers_after_the_header() -> None:
    text = "Size Used Avail\n63350874112 51539607552 9019431936\n"
    assert tc.parse_df(text) == {"size": 63350874112, "used": 51539607552, "avail": 9019431936}
    real = "1B-blocks Used Avail\n63088406528 51796385792 11292020736\n"
    assert tc.parse_df(real)["size"] == 63088406528


@pytest.mark.parametrize("text", ["", "Size Used Avail\n", "Size Used\n1 2\n", "a b c\nx y z\n"])
def test_parse_df_refuses_text_with_no_figures(text: str) -> None:
    with pytest.raises(ValueError, match="df"):
        tc.parse_df(text)


COLIMA_LINE = (
    '{"name":"default","status":"Running","arch":"aarch64","cpus":4,'
    '"memory":12884901888,"disk":42949672960,"runtime":"docker"}'
)


def test_parse_colima_list_picks_the_named_profiles_disk_in_bytes() -> None:
    other = COLIMA_LINE.replace('"default"', '"other"').replace("42949672960", "1")
    text = f"{other}\n{COLIMA_LINE}\n"
    assert tc.parse_colima_list(text) == {
        "disk_bytes": 42949672960,
        "memory_bytes": 12884901888,
        "cpus": 4,
    }
    assert tc.parse_colima_list(text, "other")["disk_bytes"] == 1
    assert tc.parse_colima_list(f"[{COLIMA_LINE}]")["cpus"] == 4


def test_parse_colima_list_refuses_a_missing_profile_or_field() -> None:
    with pytest.raises(ValueError, match="colima"):
        tc.parse_colima_list(COLIMA_LINE, "absent")
    with pytest.raises(ValueError, match="colima"):
        tc.parse_colima_list('{"name":"default","cpus":4}')
    with pytest.raises(ValueError, match="colima"):
        tc.parse_colima_list("")


# --- disk_projection and within_disk ------------------------------------------------------------


def test_the_projection_is_fifty_million_times_the_bytes_per_event_of_kafka_plus_bronze() -> None:
    projected = tc.disk_projection(
        kafka_bytes=3_000_000_000,
        kafka_records=10_000_000,
        bronze_bytes=700_000_000,
        bronze_rows=10_000_000,
    )
    assert projected == 18_500_000_000


def test_the_projection_rounds_up_in_integers() -> None:
    # 1 byte over 3 records is 50,000,000 / 3 = 16,666,666.67 -> 16,666,667 (Kafka alone).
    assert tc.disk_projection(1, 3, 0, 1) == 16_666_667
    assert tc.disk_projection(0, 1, 0, 1) == 0


@pytest.mark.parametrize("records", [0, -1])
def test_the_projection_refuses_a_zero_record_count(records: int) -> None:
    with pytest.raises(ValueError, match="records"):
        tc.disk_projection(1, records, 1, 1)
    with pytest.raises(ValueError, match="rows"):
        tc.disk_projection(1, 1, 1, records)


def test_a_projection_of_exactly_75_percent_of_the_disk_passes_and_one_byte_more_fails() -> None:
    disk = 40 * 1024**3
    exactly = disk // 4 * 3
    assert 4 * exactly == 3 * disk
    assert tc.within_disk(exactly, disk) is True
    assert tc.within_disk(exactly + 1, disk) is False
    assert tc.within_disk(0, disk) is True


# --- item12_verdict -----------------------------------------------------------------------------

GIB = 1024**3
COLIMA_DISK = 40 * GIB
DF_SIZE = 63_088_406_528
KAFKA_B = 300  # bytes per event across 18 replicas
BRONZE_B = 70


def world(
    times_s: list[int],
    rows: list[int],
    *,
    gen_rate: int = 20_000,
    kafka_per_record: int = KAFKA_B,
    primary: str = "colima",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """A consistent analysis, generator report, log-dirs figure and disk figure for these commits."""
    delivered = sum(rows)
    base, extra = divmod(delivered, 6)
    counts = [base + (1 if p < extra else 0) for p in range(6)]
    commits = [
        {
            "snapshot_id": i + 1,
            "sequence_number": i + 1,
            "committed_at_ms": T0 + t * 1000,
            "added_records": n,
        }
        for i, (t, n) in enumerate(zip(times_s, rows, strict=True))
    ]
    runs = {str(p): [[0, c]] for p, c in enumerate(counts) if c}
    started = T0 - 5_000
    span_ms = max(delivered * 1000 // gen_rate, 1)
    generator: dict[str, Any] = {
        "delivered": delivered,
        "failed": 0,
        "started_at_ms": started,
        "ended_at_ms": started + span_ms,
        "achieved_rate": gen_rate,
        "offsets_consistent": True,
        "per_partition": {
            str(p): {"start": 0, "end": c, "count": c, "runs": [[0, c]], "duplicates": 0}
            for p, c in enumerate(counts)
        },
        "knobs": {},
    }
    analysis: dict[str, Any] = {
        "commits": commits,
        "committed_events": delivered,
        "islands": {k: [list(r) for r in v] for k, v in runs.items()},
        "rows": delivered,
        "distinct_offsets": delivered,
        "distinct_event_ids": delivered,
        "kafka": {
            "partitions": {str(p): {"start": 0, "end": c} for p, c in enumerate(counts)},
            "dlq_records": 0,
        },
        "bronze": {"bytes": delivered * BRONZE_B, "rows": delivered, "files": 10},
    }
    logdirs = {"bytes": delivered * kafka_per_record, "replicas": 18, "brokers": 3}
    disk = {"colima_disk_bytes": COLIMA_DISK, "df_size_bytes": DF_SIZE, "primary": primary}
    return analysis, generator, logdirs, disk


def good() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    return world([0, 60, 120, 180, 240, 300, 360], [1_000_000] * 7)


def judge(parts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return tc.item12_verdict(*parts)  # type: ignore[arg-type]


def test_every_criterion_met_is_go_with_no_fallback_and_no_reasons() -> None:
    result = judge(good())
    assert result["verdict"] == "go"
    assert result["fallback"] is None
    assert result["reasons"] == []
    figures = result["figures"]
    assert figures["commits"] == 7
    assert figures["committed_events"] == 7_000_000
    assert figures["rows_2_to_n"] == 6_000_000
    assert figures["span_ms"] == 360_000
    assert figures["set_check"]["equal"] is True
    assert figures["sink_duplicates"] == 0
    assert figures["disk"]["primary"] == "colima"
    assert figures["projected_bytes"] == 18_500_000_000


def test_five_commits_and_five_million_events_pass() -> None:
    parts = world([0, 60, 120, 180, 240], [1_000_000] * 5)
    assert judge(parts)["verdict"] == "go"


def test_four_commits_are_a_fallback_naming_commits() -> None:
    result = judge(world([0, 60, 120, 180], [1_500_000] * 4))
    assert result["verdict"] == "fallback"
    assert result["fallback"] == tc.FALLBACK
    assert any("commits" in reason for reason in result["reasons"])


def test_four_million_nine_hundred_ninety_nine_thousand_committed_is_a_fallback_for_events() -> (
    None
):
    analysis, generator, logdirs, disk = world([0, 60, 120, 180, 240], [1_000_000] * 5)
    analysis["commits"][-1]["added_records"] -= 1
    analysis["committed_events"] -= 1
    analysis["islands"]["5"] = [[0, generator["per_partition"]["5"]["count"] - 1]]
    analysis["rows"] -= 1
    analysis["distinct_offsets"] -= 1
    analysis["distinct_event_ids"] -= 1
    result = judge((analysis, generator, logdirs, disk))
    assert result["verdict"] == "fallback"
    assert any("committed events" in reason for reason in result["reasons"])


def test_the_throughput_floor_passes_at_the_line_and_fails_one_row_under() -> None:
    at_line = world(
        [0, 250, 500, 750, 1000], [1_000_000, 3_500_000, 3_500_000, 3_500_000, 3_500_000]
    )
    assert judge(at_line)["verdict"] == "go"
    under = world([0, 250, 500, 750, 1000], [1_000_000, 3_499_999, 3_500_000, 3_500_000, 3_500_000])
    result = judge(under)
    assert result["verdict"] == "fallback"
    assert any("throughput" in reason for reason in result["reasons"])


def test_a_bronze_hole_is_a_fallback_naming_the_set_check() -> None:
    analysis, generator, logdirs, disk = good()
    count = generator["per_partition"]["0"]["count"]
    analysis["islands"]["0"] = [[0, 100], [101, count]]
    analysis["rows"] -= 1
    analysis["distinct_offsets"] -= 1
    analysis["distinct_event_ids"] -= 1
    result = judge((analysis, generator, logdirs, disk))
    assert result["verdict"] == "fallback"
    assert any("set check" in reason for reason in result["reasons"])
    assert result["figures"]["set_check"]["missing"] == 1


def test_one_row_beyond_the_distinct_offsets_is_a_fallback_naming_sink_duplicates() -> None:
    analysis, generator, logdirs, disk = good()
    analysis["rows"] += 1
    analysis["distinct_event_ids"] += 1
    result = judge((analysis, generator, logdirs, disk))
    assert result["verdict"] == "fallback"
    assert any("sink duplicates" in reason for reason in result["reasons"])
    assert result["figures"]["sink_duplicates"] == 1
    assert result["figures"]["set_check"]["equal"] is True


def test_a_projection_over_the_primary_disk_is_a_fallback_and_reports_the_other_basis() -> None:
    # 600 B per event of Kafka plus 70 B of bronze: 33.5 GB, over 75% of 40 GiB, under 75% of the df.
    parts = world([0, 60, 120, 180, 240, 300, 360], [1_000_000] * 7, kafka_per_record=600)
    result = judge(parts)
    assert result["verdict"] == "fallback"
    assert any("disk" in reason for reason in result["reasons"])
    disk = result["figures"]["disk"]
    assert disk["colima"]["within"] is False
    assert disk["df"]["within"] is True
    on_df = world(
        [0, 60, 120, 180, 240, 300, 360], [1_000_000] * 7, kafka_per_record=600, primary="df"
    )
    assert judge(on_df)["verdict"] == "go"


def test_event_duplicates_must_equal_the_generators_intended_count() -> None:
    analysis, generator, logdirs, disk = good()
    analysis["distinct_event_ids"] -= 3
    generator["knobs"] = {"intended_duplicates": 2}
    result = judge((analysis, generator, logdirs, disk))
    assert result["verdict"] == "fallback"
    assert any("event duplicates" in reason for reason in result["reasons"])
    generator["knobs"] = {"intended_duplicates": 3}
    assert judge((analysis, generator, logdirs, disk))["verdict"] == "go"


def test_malformed_offsets_are_allowed_missing_and_their_count_must_equal_the_dlq_count() -> None:
    analysis, generator, logdirs, disk = good()
    count = generator["per_partition"]["0"]["count"]
    generator["knobs"] = {"malformed": {"count": 1, "runs": {"0": [[7, 8]]}}}
    analysis["islands"]["0"] = [[0, 7], [8, count]]
    analysis["rows"] -= 1
    analysis["distinct_offsets"] -= 1
    analysis["distinct_event_ids"] -= 1
    analysis["kafka"]["dlq_records"] = 1
    result = judge((analysis, generator, logdirs, disk))
    assert result["verdict"] == "go"
    assert result["figures"]["malformed_allowed_missing"] == 1
    assert result["figures"]["set_check"]["missing_allowed"] == 1
    analysis["kafka"]["dlq_records"] = 0
    wrong = judge((analysis, generator, logdirs, disk))
    assert wrong["verdict"] == "fallback"
    assert any("malformed" in reason for reason in wrong["reasons"])


def inconclusive(parts: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    result = judge(parts)
    assert result["verdict"] == "inconclusive", result
    assert result["fallback"] is None
    assert result["reasons"]
    return result


def test_no_commits_one_commit_and_a_zero_span_are_inconclusive_never_go_or_fallback() -> None:
    analysis, generator, logdirs, disk = good()
    shortened: list[list[dict[str, Any]]] = [[], analysis["commits"][:1]]
    for commits in shortened:
        changed = {**analysis, "commits": commits}
        inconclusive((changed, generator, logdirs, disk))
    flat = [dict(c, committed_at_ms=T0) for c in analysis["commits"]]
    inconclusive(({**analysis, "commits": flat}, generator, logdirs, disk))


def test_a_missing_or_empty_generator_report_is_inconclusive() -> None:
    analysis, _, logdirs, disk = good()
    inconclusive((analysis, {}, logdirs, disk))
    inconclusive((analysis, {"delivered": 0, "per_partition": {}}, logdirs, disk))


def test_zero_delivered_events_are_inconclusive() -> None:
    analysis, generator, logdirs, disk = good()
    generator["delivered"] = 0
    inconclusive((analysis, generator, logdirs, disk))


def test_fewer_than_five_million_delivered_events_are_inconclusive_naming_the_floor() -> None:
    parts = world([0, 60, 120, 180, 240], [999_999, 1_000_000, 1_000_000, 1_000_000, 1_000_000])
    delivered = sum(parts[1]["per_partition"][str(p)]["count"] for p in range(6))
    assert delivered == 4_999_999
    result = inconclusive(parts)
    assert any("5,000,000" in reason for reason in result["reasons"])


def test_a_generator_bound_run_is_inconclusive() -> None:
    # Both the generator (10,000 events/s) and the committed throughput sit below 14,000.
    parts = world([0, 100, 200, 300, 400, 500], [1_000_000] * 6, gen_rate=10_000)
    result = inconclusive(parts)
    assert any("generator" in reason for reason in result["reasons"])


def test_a_fast_generator_with_a_slow_sink_is_a_fallback_not_inconclusive() -> None:
    parts = world([0, 100, 200, 300, 400, 500], [1_000_000] * 6, gen_rate=20_000)
    result = judge(parts)
    assert result["verdict"] == "fallback"
    assert any("throughput" in reason for reason in result["reasons"])


def test_a_missing_disk_or_log_dirs_input_is_inconclusive() -> None:
    analysis, generator, logdirs, disk = good()
    inconclusive((analysis, generator, {}, disk))
    inconclusive((analysis, generator, {"bytes": 0, "replicas": 0, "brokers": 0}, disk))
    inconclusive((analysis, generator, logdirs, {}))
    inconclusive((analysis, generator, logdirs, {**disk, "primary": "elsewhere"}))
    no_bronze = {**analysis, "bronze": {}}
    inconclusive((no_bronze, generator, logdirs, disk))


def test_a_generator_report_that_disagrees_with_kafkas_offsets_is_inconclusive() -> None:
    analysis, generator, logdirs, disk = good()
    generator["offsets_consistent"] = False
    inconclusive((analysis, generator, logdirs, disk))


def test_the_verdict_figures_carry_both_disk_percentages_and_the_per_million_bytes() -> None:
    figures = judge(good())["figures"]
    assert figures["kafka_bytes_per_million"] == 300_000_000
    assert figures["bronze_bytes_per_million"] == 70_000_000
    for basis, size in (("colima", COLIMA_DISK), ("df", DF_SIZE)):
        entry = figures["disk"][basis]
        assert entry["disk_bytes"] == size
        assert entry["threshold_bytes"] == size * 3 // 4
        assert isinstance(entry["percent"], float)


# --- CLI ----------------------------------------------------------------------------------------


def write(path: Path, payload: Any) -> Path:
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def test_the_verdict_command_prints_one_json_line_and_exits_0_for_go(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    analysis, generator, logdirs, disk = good()
    sizes = {1: [100] * 6, 2: [100] * 6, 3: [100] * 6}
    # Scale the log-dirs text so its bytes equal the fixture's figure.
    per_replica = logdirs["bytes"] // 18
    sizes = {b: [per_replica] * 6 for b in (1, 2, 3)}
    code = tc.main(
        [
            "verdict",
            "--analysis", str(write(tmp_path / "a.json", analysis)),
            "--generator", str(write(tmp_path / "g.json", generator)),
            "--logdirs", str(write(tmp_path / "l.txt", log_dirs_text(sizes))),
            "--disk", str(write(tmp_path / "d.json", disk)),
        ]
    )  # fmt: skip
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert code == 0
    assert json.loads(last)["verdict"] == "go"


def test_the_verdict_command_exits_1_when_an_input_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = tc.main(
        [
            "verdict",
            "--analysis", str(tmp_path / "missing.json"),
            "--generator", str(tmp_path / "missing.json"),
            "--logdirs", str(tmp_path / "missing.txt"),
            "--disk", str(tmp_path / "missing.json"),
        ]
    )  # fmt: skip
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 1
    assert last["verdict"] == "inconclusive"


def test_the_disk_command_reports_both_figures_and_the_primary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    colima = write(tmp_path / "c.json", COLIMA_LINE)
    df = write(tmp_path / "df.txt", "1B-blocks Used Avail\n63088406528 51796385792 11292020736\n")
    code = tc.main(["disk", "--colima-list", str(colima), "--df", str(df), "--primary", "colima"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    assert out["colima_disk_bytes"] == 42949672960
    assert out["df_size_bytes"] == 63088406528
    assert out["primary"] == "colima"


def test_the_disk_command_refuses_an_unknown_primary_with_exit_2(tmp_path: Path) -> None:
    code = tc.main(
        ["disk", "--colima-list", str(tmp_path), "--df", str(tmp_path), "--primary", "nope"]
    )
    assert code == 2


def test_the_logdirs_command_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = log_dirs_text({1: [5] * 6, 2: [5] * 6, 3: [5] * 6})
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert tc.main(["logdirs"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out == {"bytes": 90, "replicas": 18, "brokers": 3}


# --- reset and the module's own surface ---------------------------------------------------------


def test_reset_takes_no_argument_and_names_only_the_clickstream_objects() -> None:
    assert not inspect.signature(tc.reset).parameters
    assert tc.RESET_TOPICS == ("clickstream", "clickstream.dlq", "control-iceberg-clicks")
    assert tc.RESET_TABLE == "clickstream"
    assert tc.RESET_CONNECTOR == "clickstream-sink"


def test_reset_deletes_only_consumer_groups_named_for_the_clickstream_sink() -> None:
    groups = [
        "connect-clickstream-sink",
        "cg-control-clickstream-sink-1",
        "connect-bronze-sink",
        "cg-control-bronze-sink",
        "airflow",
    ]
    assert tc.groups_to_delete(groups) == [
        "cg-control-clickstream-sink-1",
        "connect-clickstream-sink",
    ]
    assert tc.groups_to_delete([]) == []


def test_the_module_never_shells_out_and_builds_sql_only_from_constants_or_identifier() -> None:
    source = Path(tc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "subprocess" not in imported
    assert "os.system" not in source
    keywords = re.compile(r"\b(SELECT|FROM|ATTACH|WHERE|SET)\b")
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        literal = "".join(str(p.value) for p in node.values if isinstance(p, ast.Constant))
        if not keywords.search(literal):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            expr = part.value
            is_constant = isinstance(expr, ast.Name) and expr.id.isupper()
            # identifier(NAME) validates a name; lakekeeper_url() is the compose-set endpoint.
            is_checked = (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Name)
                and expr.func.id in {"identifier", "lakekeeper_url"}
            )
            assert is_constant or is_checked, ast.unparse(expr)
