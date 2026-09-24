"""The secret gate goes red on a planted Shopstream token.

This re-proves Week 1's Prove step on every CI run. The token is generated at runtime in
a temp dir, so no secret ever enters git history. It needs `just tools` first; the
`just test` recipe does that. A missing gitleaks fails the test instead of skipping it.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GITLEAKS = REPO / ".tools" / "bin" / "gitleaks"
CONFIG = REPO / ".gitleaks.toml"


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
