"""The pr-title check: the real workflow step, run through bash with sample titles."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-title.yml"


def title_step() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step: dict[str, Any] = workflow["jobs"]["pr-title"]["steps"][0]
    return step


def run_check(title: str) -> int:
    step = title_step()
    env = {**os.environ, "PR_TITLE": title, "PR_TITLE_RE": step["env"]["PR_TITLE_RE"]}
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, check=False
    )
    return result.returncode


@pytest.mark.parametrize(
    "title",
    [
        "feat: week 1 scaffold",
        "feat(docs): adapt docs-kit skills",
        "docs: week 1 evidence",
        "chore(deps): update dependency ruff to v0.16.9",
        "fix!: drop the python 3.12 fallback",
        "ci(renovate): pin uv",
    ],
)
def test_accepts(title: str) -> None:
    assert run_check(title) == 0


@pytest.mark.parametrize(
    "title",
    [
        "Week 1 scaffold",
        "feat:missing space",
        "Feat: capitalised type",
        "feature: unknown type",
        "feat(Docs): capitalised scope",
        "feat: ",
        "wip",
    ],
)
def test_rejects(title: str) -> None:
    assert run_check(title) == 1
