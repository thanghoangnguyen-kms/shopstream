"""Ruff's TID251 bans wall-clock, uuid4 and module-level random calls under packages/generator only.

Each probe pipes a snippet through `ruff check --stdin-filename`, which applies the repo's
per-file-ignores to a path that never exists on disk, so no file is written into the tree.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GENERATOR_PROBE = "packages/generator/src/shopstream_generator/_probe.py"
GENERATOR_TEST_PROBE = "packages/generator/tests/_probe.py"
SCRIPT_PROBE = "scripts/_probe.py"
RANDOM_FUNCTIONS = [
    "random",
    "randrange",
    "randint",
    "choice",
    "choices",
    "shuffle",
    "sample",
    "uniform",
    "getrandbits",
    "seed",
    "gauss",
    "expovariate",
]
BANNED = [
    ("datetime", "datetime.datetime.now()"),
    ("datetime", "datetime.datetime.utcnow()"),
    ("datetime", "datetime.date.today()"),
    ("time", "time.time()"),
    ("uuid", "uuid.uuid4()"),
    *[("random", f"random.{name}()") for name in RANDOM_FUNCTIONS],
]


def codes(path: str, source: str) -> tuple[int, set[str]]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format",
            "json",
            "--stdin-filename",
            path,
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    return result.returncode, {finding["code"] for finding in json.loads(result.stdout)}


@pytest.mark.parametrize(("module", "call"), BANNED, ids=[call for _, call in BANNED])
@pytest.mark.parametrize("path", [GENERATOR_PROBE, GENERATOR_TEST_PROBE])
def test_a_banned_call_fails_under_the_generator(path: str, module: str, call: str) -> None:
    status, found = codes(path, f"import {module}\n\nX = {call}\n")
    assert status != 0
    assert "TID251" in found


@pytest.mark.parametrize(("module", "call"), BANNED, ids=[call for _, call in BANNED])
def test_the_same_call_passes_the_ban_in_scripts(module: str, call: str) -> None:
    _, found = codes(SCRIPT_PROBE, f"import {module}\n\nX = {call}\n")
    assert "TID251" not in found
