"""The canary guard rejects files that contain the erasure canary token, and only says where.

The token is generated at runtime and written to a temp env file, so it never enters git
history. Every test that prints checks the token is absent from stdout and stderr.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

import canary_guard
import pytest

REPO = Path(__file__).resolve().parents[1]


def make_canary() -> str:
    return "shpst_" + secrets.token_hex(20)


def write_env(tmp_path: Path, canary: str | None, key: str = "CANARY_TOKEN") -> Path:
    env = tmp_path / ".env"
    lines = ["POSTGRES_PASSWORD=" + secrets.token_hex(8)]
    if canary is not None:
        lines.append(f"{key}={canary}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env


def write_file(directory: Path, name: str, content: bytes) -> Path:
    path = directory / name
    path.write_bytes(content)
    return path


def test_load_canary_reads_the_token(tmp_path: Path) -> None:
    canary = make_canary()
    assert canary_guard.load_canary(write_env(tmp_path, canary)) == canary


def test_load_canary_is_none_without_a_file_or_a_key_or_a_value(tmp_path: Path) -> None:
    assert canary_guard.load_canary(tmp_path / "missing.env") is None
    assert canary_guard.load_canary(write_env(tmp_path, None)) is None
    assert canary_guard.load_canary(write_env(tmp_path, "")) is None


def test_files_containing_returns_hits_in_input_order(tmp_path: Path) -> None:
    canary = make_canary()
    first = write_file(tmp_path, "a.txt", f"x {canary} y".encode())
    clean = write_file(tmp_path, "b.txt", b"nothing here")
    last = write_file(tmp_path, "c.txt", canary.encode())
    assert canary_guard.files_containing(canary, [last, clean, first]) == [last, first]


def test_files_containing_matches_binary_content_as_bytes(tmp_path: Path) -> None:
    canary = make_canary()
    blob = write_file(tmp_path, "blob.bin", b"\x00\xff\xfe" + canary.encode() + b"\x00\x01")
    assert canary_guard.files_containing(canary, [blob]) == [blob]


def test_files_containing_ignores_unreadable_paths_and_directories(tmp_path: Path) -> None:
    canary = make_canary()
    directory = tmp_path / "somedir"
    directory.mkdir()
    hit = write_file(tmp_path, "hit.txt", canary.encode())
    paths = [tmp_path / "missing.txt", directory, hit]
    assert canary_guard.files_containing(canary, paths) == [hit]


def test_main_reports_the_offending_path_and_never_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = make_canary()
    env = write_env(tmp_path, canary)
    leak = write_file(tmp_path, "leak.txt", f"note {canary}\n".encode())
    code = canary_guard.main(["--env-file", str(env), str(leak)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.strip() == f"canary-guard: CANARY_TOKEN from infra/.env found in {leak}"
    assert canary not in captured.out + captured.err


def test_main_reports_every_offender_in_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = make_canary()
    env = write_env(tmp_path, canary)
    one = write_file(tmp_path, "one.txt", canary.encode())
    clean = write_file(tmp_path, "clean.txt", b"fine")
    two = write_file(tmp_path, "two.txt", b"prefix" + canary.encode())
    code = canary_guard.main(["--env-file", str(env), str(one), str(clean), str(two)])
    captured = capsys.readouterr()
    assert code == 1
    assert str(one) in captured.out
    assert str(two) in captured.out
    assert str(clean) not in captured.out
    assert canary not in captured.out + captured.err


def test_main_passes_a_clean_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = write_env(tmp_path, make_canary())
    clean = write_file(tmp_path, "clean.txt", b"nothing to see")
    assert canary_guard.main(["--env-file", str(env), str(clean)]) == 0
    assert capsys.readouterr().out == ""


def no_read(token: str, paths: list[Path]) -> list[Path]:
    raise AssertionError("no passed file may be read without a canary token")


@pytest.mark.parametrize("state", ["no-file", "no-key", "empty-value"])
def test_main_is_a_noop_without_a_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setattr(canary_guard, "files_containing", no_read)
    env = {
        "no-file": tmp_path / "missing.env",
        "no-key": write_env(tmp_path, None),
        "empty-value": write_env(tmp_path, ""),
    }[state]
    leak = write_file(tmp_path, "leak.txt", b"whatever")
    assert canary_guard.main(["--env-file", str(env), str(leak)]) == 0


def test_main_rejects_a_token_that_is_too_short(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    short = secrets.token_hex(canary_guard.MIN_TOKEN_LENGTH // 4)
    assert len(short) < canary_guard.MIN_TOKEN_LENGTH
    env = write_env(tmp_path, short)
    leak = write_file(tmp_path, "leak.txt", short.encode())
    code = canary_guard.main(["--env-file", str(env), str(leak)])
    captured = capsys.readouterr()
    assert code == 2
    assert "shorter than 16 characters" in captured.out + captured.err
    assert short not in captured.out + captured.err


def test_cli_run_exits_1_on_a_leak_and_prints_no_token(tmp_path: Path) -> None:
    canary = make_canary()
    env = write_env(tmp_path, canary)
    leak = write_file(tmp_path, "leak.txt", canary.encode())
    result = subprocess.run(
        [sys.executable, "scripts/canary_guard.py", "--env-file", str(env), str(leak)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert str(leak) in result.stdout
    assert canary not in result.stdout + result.stderr
