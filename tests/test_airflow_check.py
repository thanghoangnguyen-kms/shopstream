"""Unit tests for scripts/airflow_check.py: the timestamp parser, item 6's verdict rule and the argv.

No Docker and no Kafka run: the live parts (the producer, the psql reads, the wait) are exercised by
the item 6 run itself. What is pinned here is the rule that turns two messages and the DAG's runs into
go, fallback or inconclusive, and that every command is a fixed argv list, never a shell string.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import airflow_check as ac
import pytest

SCRIPT = Path(ac.__file__)
T0 = 1_790_000_000_000  # an epoch-millisecond origin; every message below is relative to it


def message(offset: int, at_s: float) -> dict[str, Any]:
    return {
        "topic": "fx.refresh",
        "partition": 0,
        "offset": offset,
        "timestamp_ms": T0 + int(at_s * 1000),
        "value": f"refresh-{offset}",
    }


def iso(at_s: float) -> str:
    """An ISO 8601 UTC text, as Postgres writes a timestamptz, for T0 plus `at_s` seconds."""
    total_us = T0 * 1000 + round(at_s * 1_000_000)
    seconds, micros = divmod(total_us, 1_000_000)
    moment = dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{micros:06d}+00:00"


def run(
    run_id: str,
    queued_s: float,
    *,
    run_type: str = "asset_triggered",
    start_s: float | None = None,
) -> dict[str, Any]:
    return {
        "id": 1,
        "run_id": run_id,
        "run_type": run_type,
        "state": "success",
        "queued_at": iso(queued_s),
        "start_date": iso(start_s if start_s is not None else queued_s + 0.5),
    }


# --- parse_iso_ms -----------------------------------------------------------------------------


def test_parse_iso_ms_reads_an_offset_timestamp_as_epoch_milliseconds() -> None:
    assert ac.parse_iso_ms("2026-10-03T10:00:01.500000+00:00") == 1_791_021_601_500


def test_parse_iso_ms_reads_a_z_suffix_the_same_way() -> None:
    assert ac.parse_iso_ms("2026-10-03T10:00:01.500000Z") == ac.parse_iso_ms(
        "2026-10-03T10:00:01.500000+00:00"
    )


def test_parse_iso_ms_applies_a_non_utc_offset() -> None:
    assert ac.parse_iso_ms("2026-10-03T17:00:01.500000+07:00") == 1_791_021_601_500


def test_parse_iso_ms_rejects_none() -> None:
    with pytest.raises(ValueError, match="timestamp"):
        ac.parse_iso_ms(None)


def test_parse_iso_ms_rejects_text_that_is_not_a_timestamp() -> None:
    with pytest.raises(ValueError, match="timestamp"):
        ac.parse_iso_ms("yesterday")


# --- item6_verdict ----------------------------------------------------------------------------


def test_two_messages_each_with_a_run_inside_the_limit_are_go() -> None:
    messages = [message(0, 0), message(1, 70)]
    runs = [run("asset_triggered__a", 2), run("asset_triggered__b", 72)]
    result = ac.item6_verdict(messages, runs)
    assert result["verdict"] == "go"
    assert result["fallback"] is None
    assert result["reasons"] == []
    assert [m["latency_s"] for m in result["matches"]] == [2.0, 2.0]
    assert [m["run_id"] for m in result["matches"]] == ["asset_triggered__a", "asset_triggered__b"]
    assert [m["offset"] for m in result["matches"]] == [0, 1]
    assert [m["start_latency_s"] for m in result["matches"]] == [2.5, 2.5]
    assert [m["state"] for m in result["matches"]] == ["success", "success"]


def test_a_run_exactly_at_the_limit_is_still_go() -> None:
    result = ac.item6_verdict(
        [message(0, 0), message(1, 70)], [run("a", 60), run("b", 130)], limit_s=60
    )
    assert result["verdict"] == "go"
    assert [m["latency_s"] for m in result["matches"]] == [60.0, 60.0]


def test_a_run_queued_61_seconds_after_its_message_is_a_fallback_naming_that_message() -> None:
    result = ac.item6_verdict([message(0, 0), message(7, 70)], [run("a", 2), run("b", 131)])
    assert result["verdict"] == "fallback"
    assert result["fallback"] == "5-minute schedule"
    assert len(result["reasons"]) == 1
    assert "offset 7" in result["reasons"][0]
    assert "61" in result["reasons"][0]
    assert [m["latency_s"] for m in result["matches"]] == [2.0, 61.0]


def test_a_message_with_no_run_is_a_fallback_naming_that_message() -> None:
    result = ac.item6_verdict([message(3, 0), message(4, 70)], [run("a", 2)])
    assert result["verdict"] == "fallback"
    assert result["fallback"] == "5-minute schedule"
    assert "offset 4" in result["reasons"][0]
    assert [m["offset"] for m in result["matches"]] == [3]


def test_one_message_only_is_inconclusive() -> None:
    result = ac.item6_verdict([message(0, 0)], [run("a", 2)])
    assert result["verdict"] == "inconclusive"
    assert result["fallback"] is None
    assert any("two messages" in reason for reason in result["reasons"])


def test_no_messages_is_inconclusive() -> None:
    assert ac.item6_verdict([], [])["verdict"] == "inconclusive"


def test_more_runs_than_messages_in_the_window_is_inconclusive() -> None:
    runs = [run("a", 2), run("b", 72), run("c", 73)]
    result = ac.item6_verdict([message(0, 0), message(1, 70)], runs)
    assert result["verdict"] == "inconclusive"
    assert any("3 asset_triggered runs" in reason for reason in result["reasons"])


def test_runs_queued_before_the_first_message_are_ignored() -> None:
    tracer = run("tracer", -300)
    result = ac.item6_verdict([message(0, 0), message(1, 70)], [tracer, run("a", 2), run("b", 72)])
    assert result["verdict"] == "go"
    assert {m["run_id"] for m in result["matches"]} == {"a", "b"}


def test_a_run_of_another_type_is_ignored() -> None:
    scheduled = run("scheduled__x", 3, run_type="scheduled")
    manual = run("manual__x", 4, run_type="manual")
    runs = [scheduled, run("a", 2), manual, run("b", 72)]
    result = ac.item6_verdict([message(0, 0), message(1, 70)], runs)
    assert result["verdict"] == "go"
    assert {m["run_id"] for m in result["matches"]} == {"a", "b"}


def test_a_run_that_cannot_be_parsed_is_inconclusive() -> None:
    broken = run("a", 2)
    broken["queued_at"] = None
    result = ac.item6_verdict([message(0, 0), message(1, 70)], [broken, run("b", 72)])
    assert result["verdict"] == "inconclusive"
    assert any("parse" in reason for reason in result["reasons"])


def test_a_message_without_a_timestamp_is_inconclusive() -> None:
    bad = message(0, 0)
    del bad["timestamp_ms"]
    result = ac.item6_verdict([bad, message(1, 70)], [run("a", 2), run("b", 72)])
    assert result["verdict"] == "inconclusive"


def test_each_message_matches_a_distinct_run() -> None:
    # One run for two messages sent 5 s apart: the second message has no run of its own.
    result = ac.item6_verdict([message(0, 0), message(1, 5)], [run("a", 2)])
    assert result["verdict"] == "fallback"
    assert [m["run_id"] for m in result["matches"]] == ["a"]


def test_the_result_is_json_serializable() -> None:
    result = ac.item6_verdict([message(0, 0), message(1, 70)], [run("a", 2), run("b", 72)])
    assert json.loads(json.dumps(result)) == result


# --- argv and constants -----------------------------------------------------------------------


def test_the_psql_argv_is_a_fixed_list_that_passes_the_sql_as_one_argument() -> None:
    argv = ac.psql_argv("SELECT 1")
    assert isinstance(argv, list)
    assert argv[0] == "docker"
    assert argv[1] == "compose"
    assert argv[-2:] == ["-c", "SELECT 1"]
    assert argv.count("SELECT 1") == 1
    for token in argv:
        assert isinstance(token, str)
    assert not {"sh", "bash", "-lc", "/bin/sh"} & set(argv)


def test_the_script_never_asks_for_a_shell() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert "os.system" not in text


def test_the_sql_reads_the_dag_runs_and_the_asset_events() -> None:
    assert ac.DAG_ID in ac.RUNS_SQL
    assert "dag_run" in ac.RUNS_SQL
    assert "json_agg" in ac.RUNS_SQL
    assert "asset_event" in ac.EVENTS_SQL
    assert ac.TOPIC == "fx.refresh"
    assert ac.LIMIT_S == 60
