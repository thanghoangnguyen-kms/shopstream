"""Docker-free tests for scripts/ram_budget.py: the limit formula, the overlap rule, the headroom
figures, item 8's verdict at every threshold and the argv builders.

The live `window` is exercised by Plans 05-04 and 05-05, not here.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import ram_budget

GIB = 1024**3
MIB = 1024**2
MEM_TOTAL = 12 * GIB
CEILING = MEM_TOTAL - GIB
RESERVE = 384 * MIB
ALL = ("generator", "spark", "dag")


# --- size_limit and proposed_limits -----------------------------------------------------------


@pytest.mark.parametrize(
    ("peak", "limit"),
    [
        (493 * MIB, 640 * MIB),  # 616.25 MiB rounds up to 640
        (int(88.5 * MIB), 128 * MIB),  # 110.6 MiB
        (int(25.6 * MIB), 32 * MIB),  # 32.0 MiB, one step
        (0, 32 * MIB),
        (1, 32 * MIB),
        (512 * MIB, 640 * MIB),  # exactly 640 MiB: no extra step
        (512 * MIB + 1, 672 * MIB),  # one byte past an exact multiple takes the next step
        (256 * MIB, 320 * MIB),
    ],
)
def test_size_limit_rounds_peak_times_1_25_up_to_a_32_mib_step(peak: int, limit: int) -> None:
    assert ram_budget.size_limit(peak) == limit


def test_size_limit_is_integer_arithmetic_and_rejects_a_negative_peak() -> None:
    assert isinstance(ram_budget.size_limit(10**15), int)
    assert ram_budget.size_limit(10**15) % ram_budget.LIMIT_STEP == 0
    with pytest.raises(ValueError, match="negative"):
        ram_budget.size_limit(-1)


def test_the_constants_are_the_stated_ones() -> None:
    assert ram_budget.LIMIT_STEP == 32 * MIB
    assert ram_budget.LIMIT_FACTOR == (5, 4)
    assert ram_budget.DAG_ID == "dbt_build_lk"
    assert ram_budget.SPARK_SERVICE == "spark-job"
    assert ram_budget.GENERATOR_SERVICE == "cdc-run"
    assert ram_budget.FALLBACK == "1 broker (FALL-03)"
    assert "dbt_build_lk" in ram_budget.DBT_RUNS_SQL
    assert "json_agg" in ram_budget.DBT_RUNS_SQL


def test_proposed_limits_size_each_sampled_service() -> None:
    got = ram_budget.proposed_limits(
        {"postgres": 200 * MIB, "karapace": 100 * MIB},
        {"postgres": 512 * MIB, "karapace": 512 * MIB},
        [],
    )
    assert got["postgres"] == {
        "sampled": True,
        "peak_bytes": 200 * MIB,
        "current_bytes": 512 * MIB,
        "proposed_bytes": 256 * MIB,
        "capped": False,
    }
    assert got["karapace"]["proposed_bytes"] == 128 * MIB
    assert list(got) == ["karapace", "postgres"]


def test_proposed_limits_keep_the_current_limit_of_a_never_sampled_service() -> None:
    got = ram_budget.proposed_limits({}, {"airflow-init": 768 * MIB}, [])
    assert got["airflow-init"] == {
        "sampled": False,
        "peak_bytes": None,
        "current_bytes": 768 * MIB,
        "proposed_bytes": 768 * MIB,
        "capped": False,
    }


def test_a_capped_service_never_gets_a_limit_above_its_current_one() -> None:
    # a Kafka broker's page cache climbs toward its limit without being at risk (owner decision 2)
    got = ram_budget.proposed_limits({"kafka-1": 1000 * MIB}, {"kafka-1": 1024 * MIB}, ["kafka-1"])
    assert got["kafka-1"]["proposed_bytes"] == 1024 * MIB
    assert got["kafka-1"]["capped"] is True
    assert got["kafka-1"]["sampled"] is True


def test_a_capped_service_whose_formula_is_lower_takes_the_formula() -> None:
    got = ram_budget.proposed_limits({"kafka-1": 100 * MIB}, {"kafka-1": 1024 * MIB}, ["kafka-1"])
    assert got["kafka-1"]["proposed_bytes"] == 128 * MIB
    assert got["kafka-1"]["capped"] is False


def test_an_uncapped_service_may_rise_above_its_current_limit() -> None:
    got = ram_budget.proposed_limits({"connect": 1400 * MIB}, {"connect": 1536 * MIB}, ["kafka-1"])
    assert got["connect"]["proposed_bytes"] == 1760 * MIB


def test_a_peak_for_a_service_with_no_current_limit_is_left_out() -> None:
    assert ram_budget.proposed_limits({"ghost": 5 * MIB}, {}, []) == {}


# --- overlap ---------------------------------------------------------------------------------


def test_three_activities_overlap_where_all_are_active() -> None:
    got = ram_budget.overlap((0, 100), [(40, 70)], (50, 90), ALL)
    assert got == {"all_active": True, "overlap_ms": 20, "window": [50, 70]}


def test_intervals_that_only_touch_do_not_overlap() -> None:
    got = ram_budget.overlap((0, 100), [(40, 70)], (100, 150), ALL)
    assert got == {"all_active": False, "overlap_ms": 0, "window": None}
    touching_spark = ram_budget.overlap((0, 100), [(100, 130)], (50, 120), ALL)
    assert touching_spark["all_active"] is False


def test_one_millisecond_of_overlap_is_enough() -> None:
    got = ram_budget.overlap((0, 101), [(0, 500)], (100, 150), ALL)
    assert got == {"all_active": True, "overlap_ms": 1, "window": [100, 101]}


def test_without_spark_required_the_generator_and_the_dag_decide() -> None:
    required = ("generator", "dag")
    assert ram_budget.overlap((0, 100), [], (50, 90), required) == {
        "all_active": True,
        "overlap_ms": 40,
        "window": [50, 90],
    }
    assert ram_budget.overlap((0, 100), [(200, 300)], (50, 90), required)["all_active"] is True


def test_a_required_activity_with_no_interval_is_never_all_active() -> None:
    assert ram_budget.overlap((0, 100), [], (50, 90), ALL)["all_active"] is False
    assert ram_budget.overlap(None, [(40, 70)], (50, 90), ALL)["all_active"] is False
    assert ram_budget.overlap((0, 100), [(40, 70)], None, ALL)["all_active"] is False


def test_an_unrequired_activity_is_ignored_even_when_it_is_missing() -> None:
    assert ram_budget.overlap((0, 100), [(40, 70)], None, ("generator", "spark"))["window"] == [
        40,
        70,
    ]
    assert ram_budget.overlap(None, [(40, 70)], (50, 90), ("spark", "dag"))["window"] == [50, 70]


def test_several_spark_runs_add_up_and_runs_that_overlap_count_once() -> None:
    got = ram_budget.overlap((0, 1000), [(100, 200), (300, 400)], (150, 350), ALL)
    assert got == {"all_active": True, "overlap_ms": 100, "window": [150, 350]}
    doubled = ram_budget.overlap((0, 1000), [(100, 300), (200, 400)], (0, 1000), ALL)
    assert doubled["overlap_ms"] == 300
    assert doubled["window"] == [100, 400]


def test_overlap_does_not_depend_on_the_order_of_the_spark_runs() -> None:
    forward = ram_budget.overlap((0, 1000), [(100, 200), (300, 400)], (150, 350), ALL)
    backward = ram_budget.overlap((0, 1000), [(300, 400), (100, 200)], (150, 350), ALL)
    assert forward == backward


# --- headroom --------------------------------------------------------------------------------


def test_headroom_is_the_distance_to_each_threshold() -> None:
    got = ram_budget.headroom(8 * GIB, 9 * GIB, CEILING)
    assert got == {
        "peak_headroom_bytes": 2 * GIB,
        "limit_headroom_bytes": CEILING - 9 * GIB,
        "w05_reserve_bytes": RESERVE,
        "w05_reserve_fits": True,
    }


def test_the_reserve_fits_at_exactly_384_mib_of_both_headrooms_and_not_one_byte_less() -> None:
    at_line = ram_budget.headroom(10 * GIB - RESERVE, CEILING - RESERVE, CEILING)
    assert at_line["w05_reserve_fits"] is True
    peak_short = ram_budget.headroom(10 * GIB - RESERVE + 1, CEILING - RESERVE, CEILING)
    assert peak_short["w05_reserve_fits"] is False
    limit_short = ram_budget.headroom(10 * GIB - RESERVE, CEILING - RESERVE + 1, CEILING)
    assert limit_short["w05_reserve_fits"] is False


def test_headroom_is_zero_at_each_threshold_and_negative_past_it() -> None:
    zero = ram_budget.headroom(10 * GIB, CEILING, CEILING)
    assert zero["peak_headroom_bytes"] == 0
    assert zero["limit_headroom_bytes"] == 0
    assert zero["w05_reserve_fits"] is False
    over = ram_budget.headroom(10 * GIB + 1, CEILING + 1, CEILING)
    assert over["peak_headroom_bytes"] == -1
    assert over["limit_headroom_bytes"] == -1


# --- item8_verdict ---------------------------------------------------------------------------

LONG_RUNNING = ["postgres", "kafka-1", "airflow-scheduler"]


def good_report(**changes: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "frames": 100,
        "peak_sum": 8 * GIB,
        "peak_ts": "2026-10-03T10:00:05Z",
        "per_service": [
            {"service": "kafka-1", "peak_bytes": 900 * MIB, "limit_bytes": 1024 * MIB},
            {"service": "postgres", "peak_bytes": 300 * MIB, "limit_bytes": 512 * MIB},
            {"service": "airflow-scheduler", "peak_bytes": 700 * MIB, "limit_bytes": 1536 * MIB},
            {"service": "spark-job", "peak_bytes": 845 * MIB, "limit_bytes": 1088 * MIB},
        ],
        "limit_total_profiles": 9 * GIB,
        "limit_total": 9 * GIB + 1088 * MIB,
        "with_services": ["spark-job"],
        "mem_total": MEM_TOTAL,
        "ceiling": CEILING,
        "inspect": [
            {"service": "postgres", "oom_killed": False, "restarts": 0},
            {"service": "spark-job", "oom_killed": False, "restarts": 0},
        ],
        "states": [
            {"service": "postgres", "status": "running", "exit_code": 0},
            {"service": "kafka-1", "status": "running", "exit_code": 0},
            {"service": "airflow-scheduler", "status": "running", "exit_code": 0},
            {"service": "spark-job", "status": "exited", "exit_code": 0},
        ],
        "events": [{"service": "spark-job", "action": "die", "exit_code": 0}],
        "breaches": [],
        "long_running": LONG_RUNNING,
        "missing_limits": [],
    }
    report.update(changes)
    return report


def good_window(**changes: Any) -> dict[str, Any]:
    window: dict[str, Any] = {
        "started_at_ms": 0,
        "ended_at_ms": 120_000,
        "generator": {"start_ms": 0, "end_ms": 100_000},
        "spark_runs": [
            {
                "container": "shopstream-spark-job-run-4f87af04bf9d",
                "start_ms": 40_000,
                "end_ms": 70_000,
                "exit": 0,
            }
        ],
        "dag_run": {
            "run_id": "manual__x",
            "state": "success",
            "start_ms": 50_000,
            "end_ms": 90_000,
        },
    }
    window.update(changes)
    return window


def judge(
    report: dict[str, Any] | None = None, window: dict[str, Any] | None = None, **keywords: Any
) -> dict[str, Any]:
    keywords.setdefault("calibrated", True)
    keywords.setdefault("required", ALL)
    return ram_budget.item8_verdict(
        report if report is not None else good_report(),
        window if window is not None else good_window(),
        **keywords,
    )


def test_a_calibrated_run_inside_every_threshold_is_go_with_headroom() -> None:
    got = judge()
    assert got["verdict"] == "go"
    assert got["fallback"] is None
    assert got["reasons"] == []
    assert got["overlap"] == {"all_active": True, "overlap_ms": 20_000, "window": [50_000, 70_000]}
    assert got["headroom"]["peak_headroom_bytes"] == 2 * GIB
    assert got["headroom"]["limit_headroom_bytes"] == CEILING - (9 * GIB + 1088 * MIB)
    assert got["headroom"]["w05_reserve_bytes"] == RESERVE


def test_exactly_ten_gib_and_exactly_the_ceiling_pass_with_zero_headroom() -> None:
    got = judge(good_report(peak_sum=10 * GIB, limit_total=CEILING))
    assert got["verdict"] == "go"
    assert got["headroom"]["peak_headroom_bytes"] == 0
    assert got["headroom"]["limit_headroom_bytes"] == 0
    assert got["headroom"]["w05_reserve_fits"] is False


def test_one_byte_over_the_peak_budget_is_a_fallback_that_names_it() -> None:
    got = judge(good_report(peak_sum=10 * GIB + 1))
    assert got["verdict"] == "fallback"
    assert got["fallback"] == "1 broker (FALL-03)"
    assert len(got["reasons"]) == 1
    assert "peak summed sample" in got["reasons"][0]


def test_one_byte_over_the_ceiling_is_a_fallback_that_names_it() -> None:
    got = judge(good_report(limit_total=CEILING + 1))
    assert got["verdict"] == "fallback"
    assert got["fallback"] == "1 broker (FALL-03)"
    assert len(got["reasons"]) == 1
    assert "sum(mem_limit)" in got["reasons"][0]


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (
            {"inspect": [{"service": "connect", "oom_killed": True, "restarts": 0}]},
            "connect was OOM killed",
        ),
        (
            {"inspect": [{"service": "connect", "oom_killed": False, "restarts": 2}]},
            "connect restarted 2 time(s)",
        ),
        (
            {"events": [{"service": "spark-job", "action": "oom", "exit_code": None}]},
            "spark-job had an OOM event",
        ),
        (
            {"events": [{"service": "spark-job", "action": "die", "exit_code": 137}]},
            "spark-job exited with code 137",
        ),
        (
            {
                "states": [
                    {"service": "postgres", "status": "running", "exit_code": 0},
                    {"service": "kafka-1", "status": "exited", "exit_code": 137},
                    {"service": "airflow-scheduler", "status": "running", "exit_code": 0},
                ]
            },
            "kafka-1",
        ),
        (
            {
                "states": [
                    {"service": "postgres", "status": "exited", "exit_code": 1},
                    {"service": "kafka-1", "status": "running", "exit_code": 0},
                    {"service": "airflow-scheduler", "status": "running", "exit_code": 0},
                ]
            },
            "postgres is exited",
        ),
    ],
)
def test_an_oom_an_exit_137_a_restart_or_a_stopped_service_is_a_fallback(
    change: dict[str, Any], needle: str
) -> None:
    got = judge(good_report(**change))
    assert got["verdict"] == "fallback"
    assert got["fallback"] == "1 broker (FALL-03)"
    assert any(needle in reason for reason in got["reasons"]), got["reasons"]


def test_a_long_running_service_with_no_container_is_a_fallback() -> None:
    states = [row for row in good_report()["states"] if row["service"] != "kafka-1"]
    got = judge(good_report(states=states))
    assert got["verdict"] == "fallback"
    assert any("kafka-1 has no container" in reason for reason in got["reasons"])


def test_a_stopped_one_shot_that_exited_zero_is_no_breach() -> None:
    assert judge()["verdict"] == "go"


def test_every_failing_threshold_is_listed_once_in_a_fixed_order() -> None:
    got = judge(
        good_report(
            peak_sum=11 * GIB,
            limit_total=CEILING + 5,
            inspect=[{"service": "connect", "oom_killed": True, "restarts": 1}],
        )
    )
    assert [reason.split()[0] for reason in got["reasons"]] == [
        "peak",
        "sum(mem_limit)",
        "container",
        "container",
    ]


def test_an_uncalibrated_run_is_inconclusive_even_when_it_would_pass_or_fail() -> None:
    passing = judge(calibrated=False)
    assert passing["verdict"] == "inconclusive"
    assert passing["fallback"] is None
    assert any("calibrat" in reason for reason in passing["reasons"])
    failing = judge(good_report(peak_sum=11 * GIB), calibrated=False)
    assert failing["verdict"] == "inconclusive"
    assert any("calibrat" in reason for reason in failing["reasons"])


@pytest.mark.parametrize("frames", [0, 4])
def test_too_few_frames_is_inconclusive(frames: int) -> None:
    got = judge(good_report(frames=frames), min_frames=5)
    assert got["verdict"] == "inconclusive"
    assert any("frames" in reason for reason in got["reasons"])


def test_exactly_min_frames_is_enough() -> None:
    assert judge(good_report(frames=5), min_frames=5)["verdict"] == "go"


def test_no_frames_is_inconclusive_at_the_default_minimum() -> None:
    assert judge(good_report(frames=0))["verdict"] == "inconclusive"


def test_activities_that_never_overlapped_are_inconclusive() -> None:
    dag = {"run_id": "x", "state": "success", "start_ms": 100_000, "end_ms": 150_000}
    got = judge(window=good_window(dag_run=dag))
    assert got["verdict"] == "inconclusive"
    assert got["overlap"]["all_active"] is False
    assert any("at one instant" in reason for reason in got["reasons"])


def test_a_spark_run_that_did_not_exit_zero_does_not_count_as_active() -> None:
    runs = [
        {
            "container": "shopstream-spark-job-run-aaaaaaaaaaaa",
            "start_ms": 40_000,
            "end_ms": 70_000,
            "exit": 1,
        }
    ]
    got = judge(window=good_window(spark_runs=runs))
    assert got["verdict"] == "inconclusive"
    assert any("Spark" in reason and "exited 0" in reason for reason in got["reasons"])


def test_no_spark_run_at_all_is_inconclusive_when_spark_is_required() -> None:
    got = judge(window=good_window(spark_runs=[]))
    assert got["verdict"] == "inconclusive"
    assert any("Spark" in reason for reason in got["reasons"])


@pytest.mark.parametrize("state", ["failed", "running", "queued", ""])
def test_a_dag_run_that_did_not_succeed_is_inconclusive(state: str) -> None:
    dag = {"run_id": "x", "state": state, "start_ms": 50_000, "end_ms": 90_000}
    got = judge(window=good_window(dag_run=dag))
    assert got["verdict"] == "inconclusive"
    assert any("DAG run" in reason and "success" in reason for reason in got["reasons"])


def test_a_missing_dag_run_is_inconclusive_when_the_dag_is_required() -> None:
    assert judge(window=good_window(dag_run=None))["verdict"] == "inconclusive"
    assert (
        judge(window={k: v for k, v in good_window().items() if k != "dag_run"})["verdict"]
        == "inconclusive"
    )


def test_a_missing_generator_is_inconclusive() -> None:
    window = {k: v for k, v in good_window().items() if k != "generator"}
    assert judge(window=window)["verdict"] == "inconclusive"


def test_an_activity_that_is_not_required_is_neither_checked_nor_needed() -> None:
    no_spark = judge(window=good_window(spark_runs=[]), required=("generator", "dag"))
    assert no_spark["verdict"] == "go"
    failed_dag = {"run_id": "x", "state": "failed", "start_ms": 50_000, "end_ms": 90_000}
    no_dag = judge(window=good_window(dag_run=failed_dag), required=("generator", "spark"))
    assert no_dag["verdict"] == "go"


@pytest.mark.parametrize("missing_key", ["frames", "peak_sum", "limit_total", "ceiling", "states"])
def test_a_report_missing_a_figure_is_inconclusive(missing_key: str) -> None:
    report = good_report()
    del report[missing_key]
    got = judge(report)
    assert got["verdict"] == "inconclusive"
    assert any(missing_key in reason for reason in got["reasons"])


def test_a_service_with_no_mem_limit_is_inconclusive() -> None:
    got = judge(good_report(missing_limits=["seaweedfs"]))
    assert got["verdict"] == "inconclusive"
    assert any("seaweedfs has no mem_limit" in reason for reason in got["reasons"])


def test_a_long_running_service_that_was_never_sampled_is_inconclusive() -> None:
    per_service = [row for row in good_report()["per_service"] if row["service"] != "postgres"]
    got = judge(good_report(per_service=per_service))
    assert got["verdict"] == "inconclusive"
    assert any("postgres was never sampled" in reason for reason in got["reasons"])


def test_a_peak_of_zero_bytes_is_inconclusive() -> None:
    got = judge(good_report(peak_sum=0))
    assert got["verdict"] == "inconclusive"
    assert any("0 B" in reason for reason in got["reasons"])


def test_an_inconclusive_reason_is_never_reported_as_a_fallback() -> None:
    got = judge(good_report(peak_sum=11 * GIB, frames=0))
    assert got["verdict"] == "inconclusive"
    assert got["fallback"] is None


def test_the_verdict_is_a_pure_function_of_its_inputs() -> None:
    assert judge() == judge()
    assert json.dumps(judge(), sort_keys=True) == json.dumps(judge(), sort_keys=True)


# --- argv builders ---------------------------------------------------------------------------


def profiles_of(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, word in enumerate(argv) if word == "--profile"]


def test_every_compose_argv_comes_from_stack_compose_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], tuple[str, ...]]] = []

    def marked(profiles: list[str], *args: str) -> list[str]:
        calls.append((list(profiles), args))
        return ["MARK", *profiles, "--", *args]

    monkeypatch.setattr(ram_budget, "compose_argv", marked)
    built = [
        ram_budget.generator_argv(1000, 100.0, 2, 7, False),
        ram_budget.spark_argv("shopstream-spark-job-run-0123456789ab"),
        ram_budget.dag_trigger_argv(),
        ram_budget.drain_argv(10, 60.0),
    ]
    assert all(argv[0] == "MARK" for argv in built)
    assert len(calls) == 4


def test_the_builders_pass_explicit_profile_lists() -> None:
    assert profiles_of(ram_budget.generator_argv(1000, 100.0, 2, 7, False)) == [
        "core",
        "streaming",
        "spike",
    ]
    assert profiles_of(ram_budget.spark_argv("shopstream-spark-job-run-0123456789ab")) == [
        "core",
        "spike",
    ]
    assert profiles_of(ram_budget.dag_trigger_argv()) == ["core", "streaming", "orchestration"]
    assert profiles_of(ram_budget.drain_argv(5, 30.0)) == ["core", "streaming", "spike"]


def test_the_spark_name_is_a_run_container_name_and_changes_each_time() -> None:
    names = {ram_budget.spark_name() for _ in range(8)}
    assert len(names) == 8
    for name in names:
        assert re.fullmatch(r"shopstream-spark-job-run-[0-9a-f]{12}", name), name


def test_the_spark_run_has_a_fixed_name_and_never_removes_itself() -> None:
    name = "shopstream-spark-job-run-0123456789ab"
    argv = ram_budget.spark_argv(name)
    assert "--rm" not in argv
    assert argv[argv.index("--name") + 1] == name
    assert "-T" in argv
    assert argv[-3:] == ["spark-job", "python", "/app/spark_v3_job.py"]


def test_the_generator_argv_carries_every_parameter_and_knobs_only_on_request() -> None:
    argv = ram_budget.generator_argv(1_000_000, 20000.0, 2, 20261006, False)
    tail = argv[argv.index("python") :]
    assert tail[:3] == ["python", "/app/clickstream_load.py", "run"]
    assert tail[tail.index("--events") + 1] == "1000000"
    assert tail[tail.index("--rate") + 1] == "20000.0"
    assert tail[tail.index("--procs") + 1] == "2"
    assert tail[tail.index("--seed") + 1] == "20261006"
    assert "--knobs" not in tail
    assert "cdc-run" in argv
    assert "--rm" in argv
    with_knobs = ram_budget.generator_argv(1000, 100.0, 2, 1, True)
    assert with_knobs[-1] == "--knobs"


def test_the_dag_trigger_argv_names_the_dag_and_asks_for_json() -> None:
    argv = ram_budget.dag_trigger_argv()
    assert argv[-6:] == ["airflow", "dags", "trigger", "dbt_build_lk", "-o", "json"]
    assert argv[argv.index("exec") + 1] == "-T"
    assert "airflow-scheduler" in argv


def test_the_drain_argv_waits_for_rows_inside_cdc_run() -> None:
    argv = ram_budget.drain_argv(999_940, 300.0)
    tail = argv[argv.index("python") :]
    assert tail[:2] == ["python", "/app/throughput_check.py"]
    assert tail[2] == "wait-rows"
    assert tail[tail.index("--expect") + 1] == "999940"
    assert tail[tail.index("--timeout") + 1] == "300.0"
    assert "cdc-run" in argv


def test_the_sampler_argv_is_this_interpreter_running_mem_report_sample(tmp_path: Path) -> None:
    out = tmp_path / "samples.jsonl"
    argv = ram_budget.sampler_argv(out)
    assert argv[0] == sys.executable
    assert Path(argv[1]).name == "mem_report.py"
    assert argv[2:] == ["sample", "--out", str(out)]


def test_remove_argv_names_each_container_and_only_those() -> None:
    names = ["shopstream-spark-job-run-0123456789ab", "shopstream-spark-job-run-fedcba987654"]
    assert ram_budget.remove_argv(names) == ["docker", "rm", *names]


def test_remove_argv_refuses_a_name_that_is_not_a_spark_run_container() -> None:
    with pytest.raises(ValueError, match="spark-job-run"):
        ram_budget.remove_argv(["shopstream-postgres-1"])
    with pytest.raises(ValueError, match="spark-job-run"):
        ram_budget.remove_argv(["shopstream-spark-job-run-0123456789ab; rm"])


BUILDERS: dict[str, Callable[[], list[str]]] = {
    "generator": lambda: ram_budget.generator_argv(10, 1.0, 2, 3, True),
    "spark": lambda: ram_budget.spark_argv("shopstream-spark-job-run-0123456789ab"),
    "dag": ram_budget.dag_trigger_argv,
    "drain": lambda: ram_budget.drain_argv(10, 5.0),
}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_argv_is_a_list_of_plain_strings_with_no_shell_syntax(name: str) -> None:
    argv = BUILDERS[name]()
    assert isinstance(argv, list)
    assert all(isinstance(word, str) for word in argv)
    for word in argv:
        assert not re.search(r"[;&|`]|\$\(", word), word


# --- the window record and the DAG helpers ------------------------------------------------------


def test_window_record_has_every_documented_key_and_nothing_secret() -> None:
    record = ram_budget.window_record(
        started_at_ms=1000,
        ended_at_ms=9000,
        generator={"start_ms": 1100, "end_ms": 5000, "delivered": 10},
        spark_runs=[{"container": "c", "start_ms": 2000, "end_ms": 3000, "exit": 0}],
        dag_run={"run_id": "r", "state": "success", "start_ms": 2500, "end_ms": 4000},
        sampler={"file": "samples.jsonl", "frames": 3},
        events_file="events.txt",
        drain={"expected": 10, "total_records": 10},
    )
    assert set(record) == {
        "started_at_ms",
        "ended_at_ms",
        "generator",
        "spark_runs",
        "dag_run",
        "sampler",
        "events_file",
        "drain",
    }
    assert record["spark_runs"] == [{"container": "c", "start_ms": 2000, "end_ms": 3000, "exit": 0}]
    assert record["dag_run"]["state"] == "success"
    assert record["events_file"] == "events.txt"
    assert json.loads(json.dumps(record)) == record


def test_window_record_keeps_spark_runs_in_start_order() -> None:
    runs = [
        {"container": "b", "start_ms": 30, "end_ms": 40, "exit": 0},
        {"container": "a", "start_ms": 10, "end_ms": 20, "exit": 0},
    ]
    record = ram_budget.window_record(
        started_at_ms=0,
        ended_at_ms=50,
        generator={},
        spark_runs=runs,
        dag_run=None,
        sampler={},
        events_file="e",
        drain=None,
    )
    assert [r["container"] for r in record["spark_runs"]] == ["a", "b"]
    assert record["dag_run"] is None


def test_parse_dag_trigger_reads_the_run_id_from_the_last_json_line() -> None:
    text = (
        "2026-10-03T15:36:35Z [info ] creating dag run loc=dag.py:615\n"
        '[{"dag_id": "dbt_build_lk", "dag_run_id": "manual__2026-10-03T15:36:35.415740+00:00",'
        ' "state": "queued"}]\n'
    )
    assert ram_budget.parse_dag_trigger(text) == "manual__2026-10-03T15:36:35.415740+00:00"


@pytest.mark.parametrize("text", ["", "no json here", "[]", '[{"dag_id": "x"}]', "{}"])
def test_parse_dag_trigger_rejects_output_with_no_run_id(text: str) -> None:
    with pytest.raises(ValueError, match="run id"):
        ram_budget.parse_dag_trigger(text)


def test_dag_run_interval_is_epoch_ms_and_none_until_both_ends_exist() -> None:
    row = {
        "run_id": "r",
        "state": "success",
        "start_date": "2026-10-03T15:36:35.730334+00:00",
        "end_date": "2026-10-03T15:36:40.708444+00:00",
    }
    start, end = ram_budget.dag_run_interval(row) or (0, 0)
    assert end - start == 4978
    assert ram_budget.dag_run_interval({**row, "end_date": None}) is None
    assert ram_budget.dag_run_interval({**row, "start_date": None}) is None
    assert ram_budget.dag_run_interval({**row, "start_date": "garbage"}) is None


# --- the command line --------------------------------------------------------------------------


def write_json(path: Path, body: Any) -> Path:
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def last_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    loaded: dict[str, Any] = json.loads(lines[-1])
    return loaded


def test_the_verdict_command_prints_one_json_line_and_exits_0_for_go(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = write_json(tmp_path / "report.json", good_report())
    window = write_json(tmp_path / "window.json", good_window())
    code = ram_budget.main(
        ["verdict", "--report", str(report), "--window", str(window), "--calibrated"]
    )
    assert code == 0
    assert last_json(capsys)["verdict"] == "go"


def test_the_verdict_command_exits_0_for_a_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = write_json(tmp_path / "report.json", good_report(peak_sum=11 * GIB))
    window = write_json(tmp_path / "window.json", good_window())
    code = ram_budget.main(
        ["verdict", "--report", str(report), "--window", str(window), "--calibrated"]
    )
    assert code == 0
    assert last_json(capsys)["verdict"] == "fallback"


def test_the_verdict_command_without_calibrated_is_inconclusive_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = write_json(tmp_path / "report.json", good_report())
    window = write_json(tmp_path / "window.json", good_window())
    code = ram_budget.main(["verdict", "--report", str(report), "--window", str(window)])
    assert code == 1
    got = last_json(capsys)
    assert got["verdict"] == "inconclusive"
    assert any("calibrat" in reason for reason in got["reasons"])


def test_the_verdict_command_reads_the_required_activities(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = write_json(tmp_path / "report.json", good_report())
    window = write_json(tmp_path / "window.json", good_window(spark_runs=[]))
    base = ["verdict", "--report", str(report), "--window", str(window), "--calibrated"]
    assert ram_budget.main(base) == 1
    capsys.readouterr()
    assert ram_budget.main([*base, "--required", "generator,dag"]) == 0
    assert last_json(capsys)["verdict"] == "go"


def test_an_unknown_required_activity_is_a_usage_error(tmp_path: Path) -> None:
    report = write_json(tmp_path / "report.json", good_report())
    window = write_json(tmp_path / "window.json", good_window())
    code = ram_budget.main(
        ["verdict", "--report", str(report), "--window", str(window), "--required", "generator,x"]
    )
    assert code == 2


def test_an_unreadable_input_is_inconclusive_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    window = write_json(tmp_path / "window.json", good_window())
    code = ram_budget.main(
        ["verdict", "--report", str(tmp_path / "absent.json"), "--window", str(window)]
    )
    assert code == 1
    assert last_json(capsys)["verdict"] == "inconclusive"


def test_the_verdict_command_needs_both_files() -> None:
    assert ram_budget.main(["verdict"]) == 2


def test_the_size_command_takes_each_services_largest_peak_across_the_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = good_report(
        per_service=[
            {"service": "postgres", "peak_bytes": 200 * MIB, "limit_bytes": 512 * MIB},
            {"service": "kafka-1", "peak_bytes": 1000 * MIB, "limit_bytes": 1024 * MIB},
            {"service": "airflow-init", "peak_bytes": None, "limit_bytes": 768 * MIB},
        ]
    )
    second = good_report(
        per_service=[
            {"service": "postgres", "peak_bytes": 300 * MIB, "limit_bytes": 512 * MIB},
            {"service": "airflow-init", "peak_bytes": 400 * MIB, "limit_bytes": 768 * MIB},
        ]
    )
    a = write_json(tmp_path / "a.json", first)
    b = write_json(tmp_path / "b.json", second)
    code = ram_budget.main(
        ["size", "--report", str(a), "--report", str(b), "--cap-services", "kafka-1"]
    )
    assert code == 0
    proposed = last_json(capsys)["proposed"]
    assert proposed["postgres"]["peak_bytes"] == 300 * MIB
    assert proposed["postgres"]["proposed_bytes"] == 384 * MIB
    assert proposed["airflow-init"]["peak_bytes"] == 400 * MIB
    assert proposed["airflow-init"]["proposed_bytes"] == 512 * MIB
    assert proposed["kafka-1"]["capped"] is True
    assert proposed["kafka-1"]["proposed_bytes"] == 1024 * MIB


def test_the_size_command_needs_a_report() -> None:
    assert ram_budget.main(["size"]) == 2


def test_the_window_command_needs_its_load_parameters() -> None:
    assert ram_budget.main(["window"]) == 2


@pytest.mark.parametrize("name", ["generator", "spark", "drain"])
def test_a_compose_run_never_restarts_the_dependencies_one_shots(name: str) -> None:
    # without --no-deps each run re-ran lakekeeper-migrate during the load (seen in the mini window)
    argv = BUILDERS[name]()
    assert argv[argv.index("run") + 1] == "--no-deps"
