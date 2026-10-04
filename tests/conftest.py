"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A git-initialised temporary repository.

    Every GIT_* variable is dropped from the child environment: a pytest run inside a git
    hook would otherwise point `git init` (and every later git call) at the real repository.
    A missing git fails loudly instead of skipping, like the repo's other tool checks.
    """
    assert shutil.which("git"), "git is missing"
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", "init", "-q"], check=True, cwd=tmp_path, env=env)
    return tmp_path
