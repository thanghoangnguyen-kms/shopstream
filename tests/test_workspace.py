"""Workspace members follow the naming convention in AGENTS.md §2.

Each `packages/<member>/` is the distribution `shopstream-<member>`, imported as
`shopstream_<member>`, and shares the root's Python requirement (uv resolves one
lockfile for the whole workspace).
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
MEMBERS = sorted(path.parent for path in (REPO / "packages").glob("*/pyproject.toml"))


def project_table(pyproject: Path) -> dict[str, Any]:
    table: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    return table


def test_workspace_has_members() -> None:
    assert MEMBERS, "packages/ has no workspace members"


@pytest.mark.parametrize("member", MEMBERS, ids=lambda path: path.name)
def test_distribution_name(member: Path) -> None:
    expected = "shopstream-" + member.name.replace("_", "-")
    assert project_table(member / "pyproject.toml")["name"] == expected


@pytest.mark.parametrize("member", MEMBERS, ids=lambda path: path.name)
def test_import_name(member: Path) -> None:
    module = "shopstream_" + member.name.replace("-", "_")
    assert (member / "src" / module / "__init__.py").is_file()
    importlib.import_module(module)


@pytest.mark.parametrize("member", MEMBERS, ids=lambda path: path.name)
def test_python_requirement_matches_root(member: Path) -> None:
    root = project_table(REPO / "pyproject.toml")["requires-python"]
    assert project_table(member / "pyproject.toml")["requires-python"] == root
