"""The secret gate goes red on a planted Shopstream token.

This re-proves Week 1's Prove step on every CI run. The token is generated at runtime in
a temp dir, so no secret ever enters git history. It needs `just tools` first; the
`just test` recipe does that. A missing gitleaks fails the test instead of skipping it.
It also proves the pre-commit hook and the CI `secrets-scan` recipe load this same config.
"""

from __future__ import annotations

import json
import secrets
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
GITLEAKS = REPO / ".tools" / "bin" / "gitleaks"
CONFIG = REPO / ".gitleaks.toml"
PRE_COMMIT = REPO / ".pre-commit-config.yaml"
JUSTFILE = REPO / "justfile"
CONFIG_ARG = ".gitleaks.toml"
GITLEAKS_CALL = [".tools/bin/gitleaks", "git"]


def hook_command() -> list[str]:
    """The tokens of the one gitleaks hook entry in .pre-commit-config.yaml."""
    loaded = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    entries = [
        hook["entry"]
        for repo in loaded["repos"]
        for hook in repo["hooks"]
        if hook.get("id") == "gitleaks"
    ]
    assert len(entries) == 1, f"expected exactly one gitleaks hook, found {len(entries)}"
    return shlex.split(entries[0])


def recipe_command(name: str) -> list[str]:
    """The tokens of the one gitleaks command in the justfile recipe `name`."""
    lines = JUSTFILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}:"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("    "):
            break
        body.append(line.strip())
    calls = [line for line in body if line.startswith(".tools/bin/gitleaks")]
    assert len(calls) == 1, f"expected one gitleaks command in `{name}`, found {len(calls)}"
    return shlex.split(calls[0])


def config_value(tokens: list[str]) -> str | None:
    """The config file a gitleaks command line names, or None."""
    for index, arg in enumerate(tokens):
        if arg == "--config":
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if arg.startswith("--config="):
            return arg.removeprefix("--config=") or None
    return None


def scan(target: Path, report: Path) -> tuple[int, list[str]]:
    assert GITLEAKS.is_file(), "gitleaks is missing: run `just tools`"
    result = subprocess.run(
        [
            str(GITLEAKS),
            "dir",
            str(target),
            "--config",
            str(CONFIG),
            "--no-banner",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    return result.returncode, [finding["RuleID"] for finding in findings]


def test_planted_token_is_caught(tmp_path: Path) -> None:
    leak = tmp_path / "leak"
    leak.mkdir()
    token = "shpst_" + secrets.token_hex(20)
    (leak / "settings.env").write_text(f"SHOPSTREAM_API_TOKEN={token}\n", encoding="utf-8")
    code, rules = scan(leak, tmp_path / "report.json")
    assert code == 1
    assert "shopstream-token" in rules


def test_clean_directory_passes(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "settings.env").write_text(
        "SHOPSTREAM_API_TOKEN=${SHOPSTREAM_API_TOKEN}\n", encoding="utf-8"
    )
    code, rules = scan(clean, tmp_path / "report.json")
    assert (code, rules) == (0, [])


def test_the_pre_commit_hook_passes_the_shopstream_config() -> None:
    command = hook_command()
    assert command[:2] == GITLEAKS_CALL
    assert config_value(command) == CONFIG_ARG


def test_the_secrets_scan_recipe_passes_the_shopstream_config() -> None:
    command = recipe_command("secrets-scan")
    assert command[:2] == GITLEAKS_CALL
    assert config_value(command) == CONFIG_ARG


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["gitleaks", "git", "--redact"], None),
        (["gitleaks", "git", "--config", "other.toml"], "other.toml"),
        (["gitleaks", "git", "--config=.gitleaks.toml"], ".gitleaks.toml"),
        (["gitleaks", "git", "--config"], None),
    ],
)
def test_config_value(tokens: list[str], expected: str | None) -> None:
    assert config_value(tokens) == expected


def test_the_scan_and_the_wiring_name_the_same_config() -> None:
    assert CONFIG == REPO / CONFIG_ARG
    assert CONFIG.is_file()
