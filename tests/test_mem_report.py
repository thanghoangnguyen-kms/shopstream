"""Docker-free tests for scripts/mem_report.py: parsers, thresholds and exit codes.

The secret in the fake compose config is generated at run time so gitleaks never sees a
literal, and it is bound to a name ruff's S105 does not treat as a password.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import mem_report
import pytest
from mem_report import Frame

GIB = 1024**3
MIB = 1024**2
MEM_TOTAL = 12515225600
# The docs sample line quoted in the research notes.
DOCS_SAMPLE = "2.352MiB / 982.5MiB"


# --- parse_size and parse_mem_usage --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.352MiB", 2466251),
        ("982.5MiB", 1030225920),
        ("1.5GiB", 1610612736),
        ("0B", 0),
        ("10kB", 10000),
        ("1.2MB", 1200000),
        ("1KiB", 1024),
        ("1TiB", 1024**4),
        ("1GB", 10**9),
        (" 3 MiB ", 3 * MIB),
    ],
)
def test_parse_size(text: str, expected: int) -> None:
    assert mem_report.parse_size(text) == expected


def test_binary_and_decimal_units_are_distinct() -> None:
    assert mem_report.parse_size("1KiB") != mem_report.parse_size("1kB")
    assert mem_report.parse_size("1MiB") != mem_report.parse_size("1MB")
    assert mem_report.parse_size("1GiB") != mem_report.parse_size("1GB")
    assert mem_report.parse_size("1MiB") == 1024 * mem_report.parse_size("1KiB")
    assert mem_report.parse_size("1MB") == 1000 * mem_report.parse_size("1kB")


@pytest.mark.parametrize("text", ["3.2XB", "5", "MiB", "", "1.2.3MiB", "1 Mib", "-1MiB", "12mb"])
def test_parse_size_rejects_unknown_input(text: str) -> None:
    with pytest.raises(ValueError, match=r"size|unit"):
        mem_report.parse_size(text)


def test_parse_size_rounds_half_to_even() -> None:
    # 0.5 B and 1.5 B are exact halves; half-to-even gives 0 and 2, half-up would give 1 and 2.
    assert mem_report.parse_size("0.5B") == 0
    assert mem_report.parse_size("1.5B") == 2
    assert mem_report.parse_size("2.5B") == 2


def test_parse_mem_usage() -> None:
    assert mem_report.parse_mem_usage(DOCS_SAMPLE) == (2466251, 1030225920)
    assert mem_report.parse_mem_usage("-- / --") is None
    assert mem_report.parse_mem_usage("0B / 0B") == (0, 0)


def test_parse_mem_usage_rejects_a_value_without_a_separator() -> None:
    with pytest.raises(ValueError, match="MemUsage"):
        mem_report.parse_mem_usage("2.352MiB")


# --- service_of ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "service"),
    [
        ("shopstream-lakekeeper-migrate-1", "lakekeeper-migrate"),
        ("/shopstream-postgres-1", "postgres"),
        ("shopstream-frankfurter-init-1", "frankfurter-init"),
        ("/shopstream-seaweedfs-12", "seaweedfs"),
        ("shopstream-spark-job-run-4f87af04bf9d", "spark-job"),
        ("/shopstream-cdc-run-run-0123456789ab", "cdc-run"),
        ("shopstream-cdc-run-1", "cdc-run"),
        ("shopstream-spark-job-run-012345678901", "spark-job"),
    ],
)
def test_service_of(name: str, service: str) -> None:
    assert mem_report.service_of(name) == service


# --- frames --------------------------------------------------------------------------------


def write_samples(path: Path, frames: list[tuple[str, dict[str, str]]]) -> None:
    lines = []
    for ts, rows in frames:
        record = {"ts": ts, "rows": [{"name": n, "mem_usage": u} for n, u in rows.items()]}
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_frames_missing_and_empty_files_yield_nothing(tmp_path: Path) -> None:
    assert mem_report.load_frames(tmp_path / "absent.jsonl") == []
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert mem_report.load_frames(empty) == []
    blank = tmp_path / "blank.jsonl"
    blank.write_text("\n\n", encoding="utf-8")
    assert mem_report.load_frames(blank) == []


def test_load_frames_leaves_out_stopped_rows(tmp_path: Path) -> None:
    path = tmp_path / "samples.jsonl"
    write_samples(
        path,
        [
            (
                "2026-09-30T10:00:00Z",
                {
                    "shopstream-postgres-1": "10MiB / 512MiB",
                    "shopstream-lakekeeper-migrate-1": "-- / --",
                },
            )
        ],
    )
    (frame,) = mem_report.load_frames(path)
    assert frame.usage == {"postgres": 10 * MIB}
    assert "lakekeeper-migrate" not in frame.usage
    assert frame.total == 10 * MIB


def test_load_frames_skips_a_write_cut_off_by_ctrl_c(tmp_path: Path) -> None:
    path = tmp_path / "samples.jsonl"
    good = json.dumps(
        {
            "ts": "2026-09-30T10:00:00Z",
            "rows": [{"name": "shopstream-a-1", "mem_usage": "1MiB / 2MiB"}],
        }
    )
    path.write_text(good + '\n{"ts": "2026-09-30T10:00:05Z", "ro', encoding="utf-8")
    assert [f.ts for f in mem_report.load_frames(path)] == ["2026-09-30T10:00:00Z"]


def test_load_frames_sums_replicas_of_one_service(tmp_path: Path) -> None:
    path = tmp_path / "samples.jsonl"
    write_samples(
        path,
        [("t", {"shopstream-worker-1": "5MiB / 9MiB", "shopstream-worker-2": "7MiB / 9MiB"})],
    )
    assert mem_report.load_frames(path)[0].usage == {"worker": 12 * MIB}


def test_service_peaks_sort_by_peak_then_name() -> None:
    frames = [
        Frame("t1", {"b": 5, "a": 5, "c": 9}),
        Frame("t2", {"b": 7, "a": 1, "d": 7}),
    ]
    assert mem_report.service_peaks(frames) == [("c", 9), ("b", 7), ("d", 7), ("a", 5)]


def test_peak_frame_takes_the_earliest_of_tied_frames() -> None:
    frames = [
        Frame("2026-09-30T10:00:10Z", {"a": 3, "b": 4}),
        Frame("2026-09-30T10:00:05Z", {"a": 6, "b": 1}),
        Frame("2026-09-30T10:00:15Z", {"a": 1}),
    ]
    assert mem_report.peak_frame(frames) == ("2026-09-30T10:00:05Z", 7)


def test_peak_frame_of_no_frames_is_an_error() -> None:
    with pytest.raises(ValueError, match="no frames"):
        mem_report.peak_frame([])


# --- limits --------------------------------------------------------------------------------


def test_mem_limit_total_sums_strings_and_ints_and_lists_missing() -> None:
    config = {
        "services": {
            "postgres": {"mem_limit": "536870912"},
            "lakekeeper": {"mem_limit": 268435456},
            "seaweedfs": {"image": "x"},
            "oddball": {"mem_limit": "512m"},
            "flag": {"mem_limit": True},
        }
    }
    total, missing = mem_report.mem_limit_total(config)
    assert total == 536870912 + 268435456
    assert missing == ["flag", "oddball", "seaweedfs"]


def test_mem_limit_total_counts_only_the_services_given() -> None:
    total, missing = mem_report.mem_limit_total({"services": {"a": {"mem_limit": "100"}}})
    assert (total, missing) == (100, [])
    assert mem_report.mem_limit_total({}) == (0, [])


# --- inspect -------------------------------------------------------------------------------


def test_parse_inspect() -> None:
    text = "/shopstream-postgres-1 false 0\n/shopstream-frankfurter-1 true 2\n\n"
    assert mem_report.parse_inspect(text) == [("postgres", False, 0), ("frankfurter", True, 2)]
    assert mem_report.parse_inspect("/shopstream-postgres-1 false 0") == [("postgres", False, 0)]


@pytest.mark.parametrize(
    "text",
    ["/shopstream-a-1 false", "/shopstream-a-1 maybe 0", "/shopstream-a-1 false x", "a b c d"],
)
def test_parse_inspect_rejects_odd_lines_without_echoing_them(text: str) -> None:
    with pytest.raises(ValueError, match="unexpected docker inspect line") as caught:
        mem_report.parse_inspect(text)
    assert text not in str(caught.value)


# --- thresholds ----------------------------------------------------------------------------

PEAK = mem_report.PEAK_BUDGET_BYTES
CEILING = MEM_TOTAL - mem_report.VM_HEADROOM_BYTES
CLEAN = [("postgres", False, 0)]


def test_thresholds_are_the_documented_values() -> None:
    assert PEAK == 10737418240
    assert mem_report.VM_HEADROOM_BYTES == 1073741824
    assert mem_report.W05_RESERVE_BYTES == 384 * MIB


def test_peak_boundary() -> None:
    assert mem_report.evaluate(PEAK, 0, [], MEM_TOTAL, CLEAN) == []
    over = mem_report.evaluate(PEAK + 1, 0, [], MEM_TOTAL, CLEAN)
    assert len(over) == 1
    assert "peak summed sample" in over[0]


def test_limit_total_boundary() -> None:
    assert mem_report.evaluate(0, CEILING, [], MEM_TOTAL, CLEAN) == []
    over = mem_report.evaluate(0, CEILING + 1, [], MEM_TOTAL, CLEAN)
    assert len(over) == 1
    assert "sum(mem_limit)" in over[0]


def test_a_missing_limit_an_oom_kill_and_a_restart_each_breach() -> None:
    assert mem_report.evaluate(0, 0, ["seaweedfs"], MEM_TOTAL, CLEAN) == [
        "service seaweedfs has no mem_limit"
    ]
    assert mem_report.evaluate(0, 0, [], MEM_TOTAL, [("frankfurter", True, 0)]) == [
        "container frankfurter was OOM killed"
    ]
    assert mem_report.evaluate(0, 0, [], MEM_TOTAL, [("lakekeeper", False, 1)]) == [
        "container lakekeeper restarted 1 time(s)"
    ]
    assert mem_report.evaluate(0, 0, [], MEM_TOTAL, [("postgres", False, 0)]) == []


# --- report and sample with the Docker calls replaced --------------------------------------


class Fakes:
    """Replacements for the three Docker calls plus the VM reading."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.canary = "fakecfg" + secrets.token_hex(16)
        self.limits: dict[str, object] = {
            "postgres": "536870912",
            "lakekeeper": "268435456",
            "frankfurter": "805306368",
        }
        self.inspect: list[tuple[str, bool, int]] = [
            ("postgres", False, 0),
            ("lakekeeper", False, 0),
            ("frankfurter", False, 0),
        ]
        self.states: list[tuple[str, str, int]] = [
            ("postgres", "running", 0),
            ("lakekeeper", "running", 0),
            ("frankfurter", "running", 0),
        ]
        self.spike_limits: dict[str, object] = {
            "spark-job": "3221225472",
            "cdc-run": "2147483648",
        }
        self.mem_total = MEM_TOTAL
        self.profiles_seen: list[list[str]] = []
        monkeypatch.setattr(mem_report, "compose_config", self.compose_config)
        monkeypatch.setattr(mem_report, "inspect_rows", self.inspect_rows)
        monkeypatch.setattr(mem_report, "state_rows", self.state_rows)
        monkeypatch.setattr(mem_report, "read_mem_total", lambda: self.mem_total)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)

    def compose_config(self, profiles: list[str]) -> dict[str, object]:
        self.profiles_seen.append(list(profiles))
        wanted = dict(self.limits)
        if "spike" in profiles:
            wanted.update(self.spike_limits)
        services: dict[str, object] = {
            name: {"mem_limit": limit, "environment": {"FAKE_MATERIAL": self.canary}}
            for name, limit in wanted.items()
        }
        return {"services": services}

    def inspect_rows(self, profiles: list[str]) -> list[tuple[str, bool, int]]:
        return list(self.inspect)

    def state_rows(self, profiles: list[str]) -> list[tuple[str, str, int]]:
        return list(self.states)


def sample_file(tmp_path: Path, postgres: str = "100MiB / 512MiB") -> Path:
    path = tmp_path / "samples.jsonl"
    write_samples(
        path,
        [
            ("2026-09-30T10:00:00Z", {"shopstream-postgres-1": postgres}),
            (
                "2026-09-30T10:00:05Z",
                {
                    "shopstream-postgres-1": postgres,
                    "shopstream-frankfurter-1": "300MiB / 768MiB",
                    "shopstream-lakekeeper-1": "19MiB / 256MiB",
                },
            ),
        ],
    )
    return path


def test_report_without_samples_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    code = mem_report.main(["report", "--samples", str(tmp_path / "absent.jsonl")])
    assert code == 2
    assert "no samples in absent.jsonl; run just mem-sample first" in capsys.readouterr().out


def test_report_with_an_empty_samples_file_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Fakes(monkeypatch)
    empty = tmp_path / "samples.jsonl"
    empty.write_text("", encoding="utf-8")
    assert mem_report.main(["report", "--samples", str(empty)]) == 2


def test_report_clean_run_prints_the_summary_and_returns_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "peak summed sample: 0.41 GiB at 2026-09-30T10:00:05Z (budget 10.00 GiB)" in out
    assert "sum(mem_limit) for profiles core: 1.50 GiB" in out
    assert "W05 reserve: 1.88 GiB" in out
    assert "minus 1 GiB leaves 10.66 GiB: limits ok" in out
    assert "postgres: OOMKilled=false RestartCount=0" in out
    assert "four significant digits" in out
    assert out.index("frankfurter") < out.index("postgres  ")  # table sorted by peak
    assert fakes.canary not in out


def test_report_never_prints_a_value_from_the_compose_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.limits["seaweedfs"] = None  # force a breach so every output branch runs
    mem_report.main(["report", "--samples", str(sample_file(tmp_path))])
    captured = capsys.readouterr()
    assert fakes.canary not in captured.out
    assert fakes.canary not in captured.err


def test_report_flags_a_service_without_a_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.limits["seaweedfs"] = None
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 1
    assert "BREACH: service seaweedfs has no mem_limit" in capsys.readouterr().out


def test_report_flags_an_oom_kill_and_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.inspect = [("frankfurter", True, 1)]
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert "BREACH: container frankfurter was OOM killed" in out
    assert "BREACH: container frankfurter restarted 1 time(s)" in out


def test_report_flags_a_peak_over_ten_gib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    path = sample_file(tmp_path, postgres="10GiB / 11GiB")
    assert mem_report.main(["report", "--samples", str(path)]) == 1
    assert "BREACH: peak summed sample" in capsys.readouterr().out


def test_report_flags_limits_over_the_vm_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.mem_total = 2 * GIB
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 1


def test_report_uses_the_same_profiles_as_just_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(monkeypatch)
    monkeypatch.setenv("COMPOSE_PROFILES", "streaming, orchestration,bootstrap,*")
    mem_report.main(["report", "--samples", str(sample_file(tmp_path))])
    # the active profiles first (the totals and the long-running set), then the same plus `spike`
    # (the per-service limit column and the --with-service names)
    assert fakes.profiles_seen == [
        ["core", "streaming", "orchestration"],
        ["core", "streaming", "orchestration", "spike"],
    ]


def test_main_returns_1_when_a_docker_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)

    def broken(_profiles: list[str]) -> dict[str, object]:
        raise mem_report.DockerError("`docker compose` failed with exit code 1")

    monkeypatch.setattr(mem_report, "compose_config", broken)
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 1
    assert "exit code 1" in capsys.readouterr().err


def test_sample_writes_frames_and_restarts_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".mem" / "samples.jsonl"
    monkeypatch.setattr(mem_report, "SAMPLES_FILE", target)
    monkeypatch.setattr(
        mem_report,
        "docker_stats_frame",
        lambda: [{"name": "shopstream-postgres-1", "mem_usage": "10MiB / 512MiB"}],
    )
    assert mem_report.main(["sample", "--duration", "0.05", "--interval", "0.02"]) == 0
    assert "frames written" in capsys.readouterr().out
    first = mem_report.load_frames(target)
    assert first
    assert all(frame.usage == {"postgres": 10 * MIB} for frame in first)
    assert mem_report.main(["sample", "--duration", "0.05", "--interval", "0.02", "--append"]) == 0
    assert len(mem_report.load_frames(target)) > len(first)
    assert mem_report.main(["sample", "--duration", "0.01", "--interval", "0.02"]) == 0
    assert len(mem_report.load_frames(target)) <= len(first)


def test_docker_stats_frame_keeps_only_shopstream_rows_and_two_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [
        {"Name": "shopstream-postgres-1", "MemUsage": DOCS_SAMPLE, "ID": "abc", "Extra": "x"},
        {"Name": "other-container", "MemUsage": DOCS_SAMPLE},
        {"Name": "shopstream-frankfurter-1", "MemUsage": "-- / --"},
    ]
    monkeypatch.setattr(
        mem_report, "_docker", lambda _argv: "\n".join(map(json.dumps, lines)) + "\n"
    )
    assert mem_report.docker_stats_frame() == [
        {"name": "shopstream-postgres-1", "mem_usage": DOCS_SAMPLE},
        {"name": "shopstream-frankfurter-1", "mem_usage": "-- / --"},
    ]


def test_inspect_rows_always_passes_a_format(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> str:
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return "/shopstream-postgres-1 false 0\n"
        return "id1\nid2\n"

    monkeypatch.setattr(mem_report, "_docker", fake)
    assert mem_report.inspect_rows(["core"]) == [("postgres", False, 0)]
    inspect_call = next(c for c in calls if c[:2] == ["docker", "inspect"])
    assert inspect_call[2:4] == ["--format", "{{.Name}} {{.State.OOMKilled}} {{.RestartCount}}"]
    assert inspect_call[4:] == ["id1", "id2"]
    listing = next(c for c in calls if c[:2] == ["docker", "compose"])
    assert "bootstrap" in listing
    assert listing[-2:] == ["ps", "-aq"]


def test_inspect_rows_with_no_containers_runs_no_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(mem_report, "_docker", fake)
    assert mem_report.inspect_rows(["core"]) == []
    assert len(calls) == 1


def test_compose_config_is_parsed_and_returned_not_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = secrets.token_hex(16)
    body = json.dumps({"services": {"a": {"mem_limit": "1", "environment": {"K": secret}}}})
    monkeypatch.setattr(mem_report, "_docker", lambda _argv: body)
    config = mem_report.compose_config(["core"])
    assert mem_report.mem_limit_total(config) == (1, [])
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


# --- container state: a kill outside the cgroup limit (WR-10) -------------------------------

CORE_CONFIG: dict[str, object] = {
    "services": {
        "postgres": {"profiles": ["core"]},
        "seaweedfs": {"profiles": ["core"]},
        "lakekeeper-migrate": {"profiles": ["core"]},
        "lakekeeper": {
            "profiles": ["core"],
            "depends_on": {
                "lakekeeper-migrate": {"condition": "service_completed_successfully"},
                "postgres": {"condition": "service_healthy"},
            },
        },
        "frankfurter-init": {"profiles": ["core"]},
        "frankfurter": {
            "profiles": ["core"],
            "depends_on": {
                "frankfurter-init": {"condition": "service_completed_successfully"},
                "postgres": {"condition": "service_healthy"},
            },
        },
        "bootstrap": {"profiles": ["bootstrap"]},
        "warehouse": {"profiles": ["bootstrap"]},
    }
}


def test_parse_state() -> None:
    text = "/shopstream-postgres-1 running 0\n/shopstream-bootstrap-1 exited 0\n\n"
    assert mem_report.parse_state(text) == [("postgres", "running", 0), ("bootstrap", "exited", 0)]
    assert mem_report.parse_state("/shopstream-a-1 exited -1") == [("a", "exited", -1)]
    assert mem_report.parse_state("/shopstream-a-1 exited 137") == [("a", "exited", 137)]
    assert mem_report.parse_state("") == []


@pytest.mark.parametrize(
    "text",
    [
        "/shopstream-a-1 running",
        "/shopstream-a-1 sleeping 0",
        "/shopstream-a-1 Running 0",
        "/shopstream-a-1 running x",
        "/shopstream-a-1 running 1.5",
        "/shopstream-a-1 running +",
        "a b c d",
    ],
)
def test_parse_state_rejects_odd_lines_without_echoing_them(text: str) -> None:
    with pytest.raises(ValueError, match="unexpected docker inspect state line") as caught:
        mem_report.parse_state(text)
    assert text not in str(caught.value)


def test_long_running_services_follow_the_one_shot_rule() -> None:
    assert mem_report.long_running_services(CORE_CONFIG) == [
        "frankfurter",
        "lakekeeper",
        "postgres",
        "seaweedfs",
    ]
    assert mem_report.long_running_services({"services": {}}) == []
    assert mem_report.long_running_services({}) == []


def test_a_container_killed_outside_its_cgroup_breaches() -> None:
    killed = [("frankfurter", False, 0)]
    assert mem_report.evaluate(
        0,
        0,
        [],
        MEM_TOTAL,
        killed,
        states=[("frankfurter", "exited", 137)],
        long_running=["frankfurter"],
    ) == [
        "container frankfurter exited with code 137 (SIGKILL, which the out-of-memory killer sends)",
        "long-running container frankfurter is exited (exit code 137), not running",
    ]


@pytest.mark.parametrize(
    "status", ["created", "paused", "restarting", "removing", "exited", "dead"]
)
def test_every_non_running_status_breaches_for_a_long_running_service(status: str) -> None:
    breaches = mem_report.evaluate(
        0, 0, [], MEM_TOTAL, CLEAN, states=[("postgres", status, 0)], long_running=["postgres"]
    )
    assert breaches == [f"long-running container postgres is {status} (exit code 0), not running"]
    assert (
        mem_report.evaluate(
            0,
            0,
            [],
            MEM_TOTAL,
            CLEAN,
            states=[("postgres", "running", 0)],
            long_running=["postgres"],
        )
        == []
    )


def test_an_exited_one_shot_is_not_a_breach() -> None:
    for code in (0, 1):
        assert (
            mem_report.evaluate(
                0,
                0,
                [],
                MEM_TOTAL,
                CLEAN,
                states=[("frankfurter-init", "exited", code)],
                long_running=[],
            )
            == []
        )
    assert mem_report.evaluate(
        0, 0, [], MEM_TOTAL, CLEAN, states=[("frankfurter-init", "exited", 137)], long_running=[]
    ) == [
        "container frankfurter-init exited with code 137 "
        "(SIGKILL, which the out-of-memory killer sends)"
    ]


def test_a_long_running_service_without_a_container_breaches() -> None:
    assert mem_report.evaluate(
        0,
        0,
        [],
        MEM_TOTAL,
        CLEAN,
        states=[("postgres", "running", 0)],
        long_running=["seaweedfs", "postgres", "lakekeeper"],
    ) == [
        "long-running service lakekeeper has no container",
        "long-running service seaweedfs has no container",
    ]
    assert mem_report.evaluate(0, 0, [], MEM_TOTAL, [], states=[], long_running=["b", "a"]) == [
        "long-running service a has no container",
        "long-running service b has no container",
    ]


def test_evaluate_without_the_new_arguments_keeps_its_result() -> None:
    assert mem_report.evaluate(0, 0, [], MEM_TOTAL, CLEAN) == []


def test_report_flags_a_vm_level_oom_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.states = [
        ("postgres", "running", 0),
        ("lakekeeper", "running", 0),
        ("frankfurter", "exited", 137),
    ]
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert "BREACH: container frankfurter exited with code 137" in out
    assert (
        "BREACH: long-running container frankfurter is exited (exit code 137), not running" in out
    )
    assert "frankfurter: OOMKilled=false RestartCount=0 Status=exited ExitCode=137" in out
    assert fakes.profiles_seen == [["core"], ["core", "spike"]]


def test_report_prints_status_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "  postgres: OOMKilled=false RestartCount=0 Status=running ExitCode=0" in out
    assert "four significant digits" in out
    assert "OOMKilled, exit code 137 or a long-running service that is not running" in out
    assert "the OOM and restart checks cover it" not in out


def test_report_prints_a_dash_for_a_container_without_a_state_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.states = [("lakekeeper", "running", 0), ("frankfurter", "running", 0)]
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert "  postgres: OOMKilled=false RestartCount=0 Status=- ExitCode=-" in out
    assert "BREACH: long-running service postgres has no container" in out


def test_state_rows_always_passes_a_format(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> str:
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return "/shopstream-postgres-1 running 0\n/shopstream-bootstrap-1 exited 0\n"
        return "id1\nid2\n"

    monkeypatch.setattr(mem_report, "_docker", fake)
    assert mem_report.state_rows(["core"]) == [
        ("postgres", "running", 0),
        ("bootstrap", "exited", 0),
    ]
    inspect_call = next(c for c in calls if c[:2] == ["docker", "inspect"])
    assert inspect_call[2:4] == ["--format", "{{.Name}} {{.State.Status}} {{.State.ExitCode}}"]
    assert inspect_call[4:] == ["id1", "id2"]
    listing = next(c for c in calls if c[:2] == ["docker", "compose"])
    assert "bootstrap" in listing
    assert listing[-2:] == ["ps", "-aq"]


def test_state_rows_with_no_containers_runs_no_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(mem_report, "_docker", fake)
    assert mem_report.state_rows(["core"]) == []
    assert len(calls) == 1


def test_inspect_format_is_the_plat_09_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mem_report.INSPECT_FORMAT == "{{.Name}} {{.State.OOMKilled}} {{.RestartCount}}"
    assert mem_report.STATE_FORMAT == "{{.Name}} {{.State.Status}} {{.State.ExitCode}}"
    assert mem_report.OOM_KILL_EXIT_CODE == 137
    states = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
    assert states == mem_report.DOCKER_STATES
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> str:
        calls.append(argv)
        return "id1\n" if argv[:2] == ["docker", "compose"] else ""

    monkeypatch.setattr(mem_report, "_docker", fake)
    mem_report.inspect_rows(["core"])
    inspect_call = next(c for c in calls if c[:2] == ["docker", "inspect"])
    assert inspect_call[2:4] == ["--format", mem_report.INSPECT_FORMAT]


# --- empty and partial samples (WR-09) ------------------------------------------------------


def test_read_samples_counts_skipped_lines(tmp_path: Path) -> None:
    path = tmp_path / "samples.jsonl"
    good = json.dumps(
        {"ts": "t1", "rows": [{"name": "shopstream-a-1", "mem_usage": "1MiB / 2MiB"}]}
    )
    bad_usage = json.dumps({"ts": "t2", "rows": [{"name": "shopstream-a-1", "mem_usage": "1MiB"}]})
    stopped = json.dumps({"ts": "t3", "rows": [{"name": "shopstream-a-1", "mem_usage": "-- / --"}]})
    path.write_text(
        "\n".join([good, "", "  ", bad_usage, stopped, '{"ts": "t4", "ro']), encoding="utf-8"
    )
    frames, skipped = mem_report.read_samples(path)
    assert [frame.ts for frame in frames] == ["t1", "t3"]
    assert skipped == 2
    assert mem_report.read_samples(tmp_path / "absent.jsonl") == ([], 0)
    assert isinstance(skipped, int)


def test_load_frames_is_read_samples_frames(tmp_path: Path) -> None:
    path = sample_file(tmp_path)
    assert mem_report.load_frames(path) == mem_report.read_samples(path)[0]


def test_coverage_breaches_sort_services_and_flag_a_zero_peak() -> None:
    frames = [Frame("t1", {"postgres": 0}), Frame("t2", {"postgres": 0})]
    assert mem_report.coverage_breaches(frames, ["seaweedfs", "postgres", "lakekeeper"]) == [
        "service lakekeeper was never sampled",
        "service seaweedfs was never sampled",
        "peak summed sample is 0 B; no memory use was observed",
    ]
    assert mem_report.coverage_breaches([Frame("t", {"postgres": 1})], ["postgres"]) == []
    assert mem_report.coverage_breaches([], []) == []
    assert mem_report.coverage_breaches([], ["postgres"]) == ["service postgres was never sampled"]


def test_report_with_only_empty_frames_returns_2_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    path = tmp_path / "samples.jsonl"
    write_samples(path, [("t1", {}), ("t2", {})])
    assert mem_report.main(["report", "--samples", str(path)]) == 2
    out = capsys.readouterr().out
    assert (
        "mem-report: no container rows in samples.jsonl; was the stack running while sampling?"
        in out
    )
    assert "mem-report: ok" not in out
    assert fakes.profiles_seen == []


def test_report_with_only_stopped_rows_returns_2_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    path = tmp_path / "samples.jsonl"
    write_samples(
        path,
        [
            ("t1", {"shopstream-postgres-1": "-- / --"}),
            ("t2", {"shopstream-postgres-1": "-- / --", "shopstream-lakekeeper-1": "-- / --"}),
        ],
    )
    assert mem_report.main(["report", "--samples", str(path)]) == 2
    assert "no container rows in samples.jsonl" in capsys.readouterr().out
    assert fakes.profiles_seen == []


def test_report_flags_a_long_running_service_that_was_never_sampled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.limits["seaweedfs"] = "805306368"
    fakes.states.append(("seaweedfs", "running", 0))
    fakes.inspect.append(("seaweedfs", False, 0))
    assert mem_report.main(["report", "--samples", str(sample_file(tmp_path))]) == 1
    out = capsys.readouterr().out
    assert "BREACH: service seaweedfs was never sampled" in out
    assert "no container" not in out


def test_a_one_shot_row_does_not_count_for_its_long_running_namesake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.limits = {"lakekeeper": "268435456"}
    fakes.inspect = [("lakekeeper", False, 0)]
    fakes.states = [("lakekeeper", "running", 0), ("lakekeeper-migrate", "exited", 0)]
    path = tmp_path / "samples.jsonl"
    write_samples(path, [("t1", {"shopstream-lakekeeper-migrate-1": "5MiB / 256MiB"})])
    assert mem_report.main(["report", "--samples", str(path)]) == 1
    out = capsys.readouterr().out
    assert "BREACH: service lakekeeper was never sampled" in out
    assert "lakekeeper-migrate" in out


def test_report_flags_a_zero_peak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    path = tmp_path / "samples.jsonl"
    rows = {
        "shopstream-postgres-1": "0B / 512MiB",
        "shopstream-lakekeeper-1": "0B / 256MiB",
        "shopstream-frankfurter-1": "0B / 768MiB",
    }
    write_samples(path, [("t1", rows), ("t2", rows)])
    assert mem_report.main(["report", "--samples", str(path)]) == 1
    out = capsys.readouterr().out
    assert "BREACH: peak summed sample is 0 B; no memory use was observed" in out
    assert "never sampled" not in out


def test_min_frames_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    path = sample_file(tmp_path)
    assert mem_report.main(["report", "--samples", str(path), "--min-frames", "2"]) == 0
    assert "mem-report: ok" in capsys.readouterr().out
    fakes.profiles_seen.clear()
    assert mem_report.main(["report", "--samples", str(path), "--min-frames", "3"]) == 2
    out = capsys.readouterr().out
    assert (
        "mem-report: 2 frames with container rows in samples.jsonl, fewer than --min-frames 3"
        in out
    )
    assert "mem-report: ok" not in out
    assert fakes.profiles_seen == []


def test_min_frames_counts_only_frames_with_container_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    path = tmp_path / "samples.jsonl"
    row = {"shopstream-postgres-1": "1MiB / 2MiB"}
    write_samples(path, [("t1", row), ("t2", {}), ("t3", row)])
    assert mem_report.main(["report", "--samples", str(path), "--min-frames", "3"]) == 2
    assert "2 frames with container rows" in capsys.readouterr().out
    assert mem_report.main(["report", "--samples", str(path), "--min-frames", "2"]) in {0, 1}


@pytest.mark.parametrize("value", ["0", "-1", "x", "1.5", ""])
def test_min_frames_rejects_non_positive_values(value: str) -> None:
    with pytest.raises(SystemExit) as caught:
        mem_report.main(["report", "--min-frames", value])
    assert caught.value.code == 2


def test_report_header_states_the_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    path = tmp_path / "samples.jsonl"
    row = {"shopstream-postgres-1": "1MiB / 2MiB"}
    write_samples(path, [("2026-09-30T10:00:00Z", row), ("2026-09-30T10:00:05Z", {})])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": "2026-09-30T10:00:10Z", "rows": [{"name": "x"}]}) + "\n")
        handle.write(
            json.dumps(
                {
                    "ts": "2026-09-30T10:00:15Z",
                    "rows": [{"name": "shopstream-postgres-1", "mem_usage": "2MiB / 4MiB"}],
                }
            )
            + "\n"
        )
        handle.write('{"ts": "cut off')
    mem_report.main(["report", "--samples", str(path)])
    assert (
        "mem-report: 2 frames with container rows, 2026-09-30T10:00:00Z to 2026-09-30T10:00:15Z; "
        "2 lines skipped, 1 frames without container rows"
    ) in capsys.readouterr().out


def test_breaches_come_out_in_the_documented_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.mem_total = 2 * GIB
    fakes.limits["seaweedfs"] = None
    fakes.limits["zeta"] = "100"
    fakes.inspect = [("frankfurter", True, 2)]
    fakes.states = [
        ("postgres", "running", 0),
        ("lakekeeper", "exited", 137),
        ("frankfurter", "running", 0),
    ]
    path = sample_file(tmp_path, postgres="10GiB / 11GiB")
    assert mem_report.main(["report", "--samples", str(path)]) == 1
    breaches = [
        line.removeprefix("BREACH: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("BREACH: ")
    ]
    starts = [
        "peak summed sample",
        "sum(mem_limit)",
        "service seaweedfs has no mem_limit",
        "container frankfurter was OOM killed",
        "container frankfurter restarted 2 time(s)",
        "container lakekeeper exited with code 137",
        "long-running container lakekeeper is exited (exit code 137), not running",
        "long-running service seaweedfs has no container",
        "long-running service zeta has no container",
        "service seaweedfs was never sampled",
        "service zeta was never sampled",
    ]
    assert len(breaches) == len(starts)
    for breach, start in zip(breaches, starts, strict=True):
        assert breach.startswith(start), (breach, start)


# --- compose run containers, spike one-offs and OOM events (Phase 5, item 8) ----------------

RUN_FRAMES = [
    (
        "2026-10-03T10:00:00Z",
        {"shopstream-postgres-1": "100MiB / 512MiB"},
    ),
    (
        "2026-10-03T10:00:05Z",
        {
            "shopstream-postgres-1": "120MiB / 512MiB",
            "shopstream-spark-job-run-4f87af04bf9d": "800MiB / 3GiB",
            "shopstream-cdc-run-run-0123456789ab": "300MiB / 2GiB",
            "shopstream-frankfurter-1": "300MiB / 768MiB",
            "shopstream-lakekeeper-1": "19MiB / 256MiB",
        },
    ),
]


def run_samples(tmp_path: Path) -> Path:
    path = tmp_path / "samples.jsonl"
    write_samples(path, RUN_FRAMES)
    return path


def test_events_format_and_argv_always_carry_an_explicit_format() -> None:
    assert mem_report.EVENTS_FORMAT == (
        '{{.Time}} {{.Action}} {{.Actor.Attributes.name}} {{with index .Actor.Attributes "exitCode"}}'
        "{{.}}{{else}}-{{end}}"
    )
    argv = mem_report.events_argv("1791040000")
    assert argv[:4] == ["docker", "events", "--since", "1791040000"]
    assert argv[argv.index("--format") + 1] == mem_report.EVENTS_FORMAT
    assert "label=com.docker.compose.project=shopstream" in argv
    assert [argv[i + 1] for i, word in enumerate(argv) if word == "--filter"] == [
        "label=com.docker.compose.project=shopstream",
        "event=oom",
        "event=die",
    ]
    assert all(isinstance(word, str) for word in argv)


def test_parse_events_keeps_capture_order_and_maps_run_containers() -> None:
    text = (
        "1791040003 die shopstream-spark-job-run-4f87af04bf9d 0\n"
        "1791040001 oom shopstream-airflow-scheduler-1 -\n"
        "\n"
        "1791040002 die shopstream-airflow-scheduler-1 137\n"
    )
    assert mem_report.parse_events(text) == [
        ("spark-job", "die", 0),
        ("airflow-scheduler", "oom", None),
        ("airflow-scheduler", "die", 137),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "1791040001 kill shopstream-postgres-1 -",
        "1791040001 die shopstream-postgres-1",
        "1791040001 die shopstream-postgres-1 x",
        "oom shopstream-postgres-1 -",
        "1791040001 die shopstream-postgres-1 137 extra",
    ],
)
def test_parse_events_rejects_an_unknown_action_and_a_malformed_line(text: str) -> None:
    with pytest.raises(ValueError, match="docker events"):
        mem_report.parse_events(text)


def test_an_oom_event_and_an_exit_137_die_each_breach() -> None:
    oom = mem_report.evaluate(0, 0, [], MEM_TOTAL, [], events=[("spark-job", "oom", None)])
    assert oom == ["container spark-job had an OOM event"]
    killed = mem_report.evaluate(0, 0, [], MEM_TOTAL, [], events=[("spark-job", "die", 137)])
    assert len(killed) == 1
    assert killed[0].startswith("container spark-job exited with code 137")
    assert "events capture" in killed[0]


@pytest.mark.parametrize("code", [0, 1, 2, 143])
def test_a_die_with_another_exit_code_is_not_a_breach(code: int) -> None:
    assert mem_report.evaluate(0, 0, [], MEM_TOTAL, [], events=[("spark-job", "die", code)]) == []


def test_event_breaches_follow_the_capture_order() -> None:
    events: list[tuple[str, str, int | None]] = [
        ("zeta", "die", 137),
        ("alpha", "oom", None),
    ]
    got = mem_report.evaluate(0, 0, [], MEM_TOTAL, [], events=events)
    assert [b.split()[1] for b in got] == ["zeta", "alpha"]


def test_inspect_and_state_cover_the_spike_containers_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str]) -> str:
        calls.append(argv)
        return "id1\n" if argv[:2] == ["docker", "compose"] else ""

    monkeypatch.setattr(mem_report, "_docker", fake)
    mem_report.inspect_rows(["core", "streaming"])
    listing = next(c for c in calls if c[:2] == ["docker", "compose"])
    profiles = [listing[i + 1] for i, word in enumerate(listing) if word == "--profile"]
    assert profiles == ["core", "streaming", "bootstrap", "spike"]
    assert listing[-2:] == ["ps", "-aq"]


def test_the_table_shows_a_spike_services_limit_for_its_run_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    mem_report.main(["report", "--samples", str(run_samples(tmp_path))])
    out = capsys.readouterr().out
    spark = next(line for line in out.splitlines() if line.startswith("spark-job "))
    assert spark.split() == ["spark-job", "800.0", "3072.0"]
    cdc = next(line for line in out.splitlines() if line.startswith("cdc-run "))
    assert cdc.split() == ["cdc-run", "300.0", "2048.0"]
    assert "spark-job-run" not in out


def test_without_with_service_the_total_is_the_active_profiles_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    assert mem_report.main(["report", "--samples", str(run_samples(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "sum(mem_limit) for profiles core: 1.50 GiB" in out
    assert "sum(mem_limit) with spark-job" not in out
    assert "sum(mem_limit) with cdc-run" not in out


def test_with_service_adds_the_named_spike_limit_and_judges_the_ceiling_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.mem_total = 5 * GIB  # ceiling 4 GiB: the profiles' 1.5 GiB fits, 1.5 + 3 does not
    samples = run_samples(tmp_path)
    assert mem_report.main(["report", "--samples", str(samples)]) == 0
    capsys.readouterr()
    code = mem_report.main(["report", "--samples", str(samples), "--with-service", "spark-job"])
    out = capsys.readouterr().out
    assert code == 1
    assert "sum(mem_limit) for profiles core: 1.50 GiB" in out
    assert "sum(mem_limit) with spark-job: 4.50 GiB" in out
    assert "limits over" in out
    assert "BREACH: sum(mem_limit) 4831838208 B is over MemTotal minus 1 GiB" in out


def test_with_service_is_repeatable_and_sums_each_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    args = ["--with-service", "spark-job", "--with-service", "cdc-run"]
    assert mem_report.main(["report", "--samples", str(run_samples(tmp_path)), *args]) == 0
    assert "sum(mem_limit) with spark-job, cdc-run: 6.50 GiB" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["nosuch", "postgres"])
def test_with_service_rejects_an_unknown_or_an_already_active_name(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Fakes(monkeypatch)
    code = mem_report.main(
        ["report", "--samples", str(run_samples(tmp_path)), "--with-service", name]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert name in out
    assert "--with-service" in out


def test_spike_services_are_not_long_running_for_the_state_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    code = mem_report.main(
        ["report", "--samples", str(run_samples(tmp_path)), "--with-service", "spark-job"]
    )
    assert code == 0
    assert "has no container" not in capsys.readouterr().out


def write_events(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "events.txt"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("1791040001 oom shopstream-spark-job-run-4f87af04bf9d -\n", "had an OOM event"),
        ("1791040001 die shopstream-spark-job-run-4f87af04bf9d 137\n", "exited with code 137"),
    ],
)
def test_the_events_capture_turns_oom_and_137_into_breaches(
    text: str,
    needle: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Fakes(monkeypatch)
    events = write_events(tmp_path, text)
    code = mem_report.main(
        ["report", "--samples", str(run_samples(tmp_path)), "--events", str(events)]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert f"BREACH: container spark-job {needle}" in out


def test_a_clean_events_capture_is_no_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Fakes(monkeypatch)
    events = write_events(tmp_path, "1791040001 die shopstream-spark-job-run-4f87af04bf9d 0\n")
    assert (
        mem_report.main(
            ["report", "--samples", str(run_samples(tmp_path)), "--events", str(events)]
        )
        == 0
    )


def test_an_unreadable_events_capture_is_an_error_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Fakes(monkeypatch)
    events = write_events(tmp_path, "not an event line\n")
    code = mem_report.main(
        ["report", "--samples", str(run_samples(tmp_path)), "--events", str(events)]
    )
    assert code == 1
    assert "docker events" in capsys.readouterr().err


def test_json_out_holds_every_figure_and_is_identical_on_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(monkeypatch)
    events = write_events(tmp_path, "1791040001 die shopstream-spark-job-run-4f87af04bf9d 0\n")
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    for target in (first, second):
        args = [
            "report",
            "--samples",
            str(run_samples(tmp_path)),
            "--events",
            str(events),
            "--with-service",
            "spark-job",
            "--json-out",
            str(target),
        ]
        assert mem_report.main(args) == 0
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    report = json.loads(first.read_text(encoding="utf-8"))
    assert {
        "frames",
        "peak_sum",
        "peak_ts",
        "per_service",
        "limit_total_profiles",
        "limit_total",
        "with_services",
        "mem_total",
        "ceiling",
        "inspect",
        "states",
        "events",
        "breaches",
        "long_running",
    } <= set(report)
    assert report["frames"] == 2
    assert report["peak_ts"] == "2026-10-03T10:00:05Z"
    assert report["peak_sum"] == sum(
        mem_report.parse_size(x.split(" / ")[0]) for x in RUN_FRAMES[1][1].values()
    )
    assert report["limit_total_profiles"] == 536870912 + 268435456 + 805306368
    assert report["limit_total"] == report["limit_total_profiles"] + 3221225472
    assert report["with_services"] == ["spark-job"]
    assert report["mem_total"] == MEM_TOTAL
    assert report["ceiling"] == MEM_TOTAL - GIB
    assert report["events"] == [{"service": "spark-job", "action": "die", "exit_code": 0}]
    assert report["breaches"] == []
    # inspect and states are sorted by service, so the file does not depend on docker's list order
    assert [row["service"] for row in report["inspect"]] == [
        "frankfurter",
        "lakekeeper",
        "postgres",
    ]
    assert {row["service"]: row for row in report["inspect"]}["postgres"] == {
        "service": "postgres",
        "oom_killed": False,
        "restarts": 0,
    }
    assert {row["service"]: row for row in report["states"]}["postgres"] == {
        "service": "postgres",
        "status": "running",
        "exit_code": 0,
    }
    names = [row["service"] for row in report["per_service"]]
    assert names == ["spark-job", "cdc-run", "frankfurter", "postgres", "lakekeeper"]
    assert report["per_service"][0] == {
        "service": "spark-job",
        "peak_bytes": 800 * MIB,
        "limit_bytes": 3221225472,
    }
    assert fakes.canary not in first.read_text(encoding="utf-8")


def test_json_out_is_written_when_the_report_breaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = Fakes(monkeypatch)
    fakes.inspect = [("frankfurter", True, 1)]
    target = tmp_path / "out.json"
    code = mem_report.main(
        ["report", "--samples", str(run_samples(tmp_path)), "--json-out", str(target)]
    )
    assert code == 1
    report = json.loads(target.read_text(encoding="utf-8"))
    assert any("OOM killed" in b for b in report["breaches"])
    assert report["with_services"] == []
    assert report["limit_total"] == report["limit_total_profiles"]


def test_sample_out_writes_to_the_named_file_and_not_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = tmp_path / ".mem" / "samples.jsonl"
    monkeypatch.setattr(mem_report, "SAMPLES_FILE", default)
    monkeypatch.setattr(
        mem_report,
        "docker_stats_frame",
        lambda: [{"name": "shopstream-postgres-1", "mem_usage": "10MiB / 512MiB"}],
    )
    out = tmp_path / "run" / "mini.jsonl"
    assert (
        mem_report.main(["sample", "--duration", "0.05", "--interval", "0.02", "--out", str(out)])
        == 0
    )
    assert mem_report.load_frames(out)
    assert not default.exists()


def test_json_out_orders_equal_peaks_by_service_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Fakes(monkeypatch)
    path = tmp_path / "samples.jsonl"
    write_samples(
        path,
        [
            (
                "2026-10-03T10:00:00Z",
                {
                    "shopstream-postgres-1": "100MiB / 512MiB",
                    "shopstream-lakekeeper-1": "100MiB / 256MiB",
                    "shopstream-frankfurter-1": "100MiB / 768MiB",
                },
            )
        ],
    )
    target = tmp_path / "out.json"
    mem_report.main(["report", "--samples", str(path), "--json-out", str(target)])
    report = json.loads(target.read_text(encoding="utf-8"))
    assert [row["service"] for row in report["per_service"]] == [
        "frankfurter",
        "lakekeeper",
        "postgres",
    ]


def test_the_earliest_of_equal_peak_frames_is_the_peak_in_the_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Fakes(monkeypatch)
    path = tmp_path / "samples.jsonl"
    row = {"shopstream-postgres-1": "100MiB / 512MiB"}
    write_samples(
        path,
        [
            ("2026-10-03T10:00:10Z", row),
            ("2026-10-03T10:00:00Z", row),
            ("2026-10-03T10:00:05Z", row),
        ],
    )
    target = tmp_path / "out.json"
    mem_report.main(["report", "--samples", str(path), "--json-out", str(target)])
    assert json.loads(target.read_text(encoding="utf-8"))["peak_ts"] == "2026-10-03T10:00:00Z"
