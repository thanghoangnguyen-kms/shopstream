"""The golden run in two subprocesses must agree with each other and with the committed golden.

One leg runs with `PYTHONHASHSEED=0` and `TZ=UTC`, the other with `PYTHONHASHSEED=12345` and
`TZ=Asia/Ho_Chi_Minh`. The legs prove their own environment took effect (a runner without
tzdata would silently fall back to UTC, and `python -I` would drop the hash seed), so a leg
whose override didn't apply fails rather than passing vacuously.

This test only reads the golden. It writes under `tmp_path` and nowhere else: the manifest
changes only through `just generator-golden`, in a commit that says why (ADR-005). A failure
prints stream names, counts and digests, never a record.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from shopstream_generator.manifest import Manifest, mismatch_report

REPO = Path(__file__).resolve().parents[3]
GOLDEN = REPO / "packages/generator/tests/golden"
LEG_UTC = {"PYTHONHASHSEED": "0", "TZ": "UTC"}
LEG_HO_CHI_MINH = {"PYTHONHASHSEED": "12345", "TZ": "Asia/Ho_Chi_Minh"}
# 2025-07-01T00:00Z. A 2025 instant, because Linux tzdata gives +08 for Ho Chi Minh at epoch 0.
PROBE_INSTANT = 1751328000
PROBE = f"import time; print(time.strftime('%z', time.localtime({PROBE_INSTANT})), hash('a'))"


def run_leg(env: dict[str, str], out: Path) -> bytes:
    """Run the golden entry point under `env` and return the manifest bytes it wrote.

    There is no isolation flag in the argv: `-I` would drop PYTHONHASHSEED.
    """
    subprocess.run(
        [
            sys.executable,
            "-m",
            "shopstream_generator.golden",
            "--config",
            str(GOLDEN / "config.json"),
            "--out",
            str(out),
        ],
        env={**os.environ, **env},
        cwd=REPO,
        check=True,
        capture_output=True,
        timeout=120,
    )
    return out.read_bytes()


def probe(env: dict[str, str]) -> tuple[str, str]:
    """The UTC offset and `hash('a')` a fresh interpreter sees under `env`."""
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        env={**os.environ, **env},
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    offset, hashed = result.stdout.split()
    return offset, hashed


def test_two_processes_match_the_golden(tmp_path: Path) -> None:
    probes = {}
    for label, env, offset in (
        ("utc", LEG_UTC, "+0000"),
        ("ho-chi-minh", LEG_HO_CHI_MINH, "+0700"),
    ):
        first, second = probe(env), probe(env)
        assert first[0] == offset, f"{label}: the TZ override did not take effect"
        assert first[1] == second[1], f"{label}: PYTHONHASHSEED did not reach the child"
        probes[label] = first[1]
    assert probes["utc"] != probes["ho-chi-minh"], "the legs share a hash seed"

    golden_path = GOLDEN / "manifest.json"
    if not golden_path.is_file() or golden_path.stat().st_size == 0:
        pytest.fail("the golden manifest is missing or empty: run `just generator-golden`")
    golden = golden_path.read_bytes()
    leg_a = run_leg(LEG_UTC, tmp_path / "a.json")
    leg_b = run_leg(LEG_HO_CHI_MINH, tmp_path / "b.json")
    if leg_a != leg_b:
        pytest.fail(
            "the two legs disagree\n"
            + mismatch_report(Manifest.from_bytes(leg_a), Manifest.from_bytes(leg_b))
        )
    if leg_a != golden:
        pytest.fail(
            "the legs differ from the committed golden\n"
            + mismatch_report(Manifest.from_bytes(golden), Manifest.from_bytes(leg_a))
        )
