"""Unit tests for scripts/connect_kill.py: the log-line parser and the docker argv lists.

No Docker runs: the harness's live parts (following the log, the kill, the restart) are exercised
by the item 5 run itself. What is pinned here is what must never change silently: which Connect log
lines mean which commit stage, and that every command is a fixed argv list, never a shell string.
"""

from __future__ import annotations

import re
from pathlib import Path

import connect_kill as ck
import pytest

UUID_A = "0f8e7c2a-1b2c-4d3e-8f90-123456789abc"
SCRIPT = Path(ck.__file__)


def test_an_initiated_line_returns_the_stage_and_the_commit_id() -> None:
    line = f"2026-10-03 06:00:01,123 INFO  [bronze-sink|task-0] Coordinator bronze-sink-0 initiated commit {UUID_A} (Coordinator)"
    assert ck.parse_line(line) == ("initiated", UUID_A)


def test_a_ready_line_returns_the_stage_and_the_commit_id() -> None:
    line = f"INFO Commit {UUID_A} ready, received responses for all 3 partitions"
    assert ck.parse_line(line) == ("ready", UUID_A)


def test_a_completed_line_without_a_commit_id_returns_none_for_the_id() -> None:
    line = "INFO Coordinator bronze-sink-0 completed commit to table bronze.orders"
    assert ck.parse_line(line) == ("completed", None)


def test_a_completed_line_that_names_a_commit_id_returns_it() -> None:
    line = f"INFO Coordinator bronze-sink-0 completed commit to table x, commit {UUID_A}"
    assert ck.parse_line(line) == ("completed", UUID_A)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "INFO Debezium streaming started",
        f"INFO Commit {UUID_A} not ready, received responses for 4 of 12 partitions",
        "INFO initiated commit not-a-uuid",
    ],
)
def test_an_unrelated_line_returns_none_none(line: str) -> None:
    assert ck.parse_line(line) == (None, None)


def test_the_three_stages_are_the_ones_the_cli_offers() -> None:
    assert set(ck.STAGES) == {"initiated", "ready", "completed"}


def test_the_compose_argv_names_the_core_and_streaming_profiles_and_this_repos_file() -> None:
    argv = ck.COMPOSE_ARGV
    assert argv[:2] == ["docker", "compose"]
    assert argv[argv.index("-f") + 1].endswith("infra/compose.yaml")
    assert [argv[i + 1] for i, word in enumerate(argv) if word == "--profile"] == [
        "core",
        "streaming",
    ]


def test_every_command_is_a_fixed_argv_list_of_plain_strings() -> None:
    commands = [
        ck.logs_argv("1s"),
        ck.kill_argv(),
        ck.start_argv(),
        ck.status_argv("shopstream-cdc"),
    ]
    for argv in commands:
        assert isinstance(argv, list)
        assert all(isinstance(word, str) and word for word in argv)
        assert not any(re.search(r"[;&|`$<>]", word) for word in argv), argv
    assert ck.kill_argv()[-4:] == ["kill", "-s", "KILL", "connect"]
    assert ck.logs_argv("1s")[-1] == "connect"


def test_the_script_never_asks_subprocess_for_a_shell() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert "os.system" not in text


def test_the_status_command_polls_the_named_connector_inside_the_connect_container() -> None:
    argv = ck.status_argv("bronze-sink")
    assert argv[-1] == "http://localhost:8083/connectors/bronze-sink/status"
    assert argv[len(ck.COMPOSE_ARGV)] == "exec"


def test_a_connector_name_that_could_change_the_url_is_refused() -> None:
    with pytest.raises(ValueError, match="connector name"):
        ck.status_argv("../../admin")


def test_both_connectors_must_be_running_with_every_task_running() -> None:
    running = {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}]}
    assert ck.is_running(running)
    assert not ck.is_running({"connector": {"state": "RUNNING"}, "tasks": []})
    assert not ck.is_running({"connector": {"state": "RUNNING"}, "tasks": [{"state": "FAILED"}]})
    assert not ck.is_running(
        {"connector": {"state": "UNASSIGNED"}, "tasks": [{"state": "RUNNING"}]}
    )
    assert not ck.is_running({})
