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
        self.mem_total = MEM_TOTAL
        self.profiles_seen: list[list[str]] = []
        monkeypatch.setattr(mem_report, "compose_config", self.compose_config)
        monkeypatch.setattr(mem_report, "inspect_rows", self.inspect_rows)
        monkeypatch.setattr(mem_report, "read_mem_total", lambda: self.mem_total)
        monkeypatch.delenv("COMPOSE_PROFILES", raising=False)

    def compose_config(self, profiles: list[str]) -> dict[str, object]:
        self.profiles_seen.append(list(profiles))
        services: dict[str, object] = {
            name: {"mem_limit": limit, "environment": {"FAKE_MATERIAL": self.canary}}
            for name, limit in self.limits.items()
        }
        return {"services": services}

    def inspect_rows(self, profiles: list[str]) -> list[tuple[str, bool, int]]:
        return list(self.inspect)


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
    assert fakes.profiles_seen == [["core", "streaming", "orchestration"]]


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
