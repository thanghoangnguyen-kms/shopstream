"""Pins the contracts/ uv project: datacontract-cli stays out of the workspace lock.

Goes red if contracts/ stops pinning datacontract-cli==1.2.1 with no extras, stops being a virtual
project with the 3-day cooldown, its lock loses the P3D span or the pinned version, the workspace
members change from packages/*, or the workspace uv.lock gains datacontract-cli. The tests read
files only and use no network.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CONTRACTS = REPO / "contracts"


def load_toml(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    return loaded


def locked(lock: dict[str, Any]) -> dict[str, str]:
    return {p["name"]: p["version"] for p in lock["package"] if "version" in p}


def test_the_project_pins_datacontract_cli_with_no_extras() -> None:
    project = load_toml(CONTRACTS / "pyproject.toml")
    assert project["project"]["dependencies"] == ["datacontract-cli==1.2.1"]


def test_the_project_is_virtual_with_the_three_day_cooldown() -> None:
    uv = load_toml(CONTRACTS / "pyproject.toml")["tool"]["uv"]
    assert uv["package"] is False
    assert uv["exclude-newer"] == "3 days"


def test_the_lock_records_the_span_and_the_pinned_version() -> None:
    lock = load_toml(CONTRACTS / "uv.lock")
    assert lock["options"]["exclude-newer-span"] == "P3D"
    assert locked(lock)["datacontract-cli"] == "1.2.1"


def test_the_workspace_leaves_contracts_out_and_never_locks_datacontract_cli() -> None:
    root = load_toml(REPO / "pyproject.toml")
    assert root["tool"]["uv"]["workspace"]["members"] == ["packages/*"]
    assert "datacontract-cli" not in locked(load_toml(REPO / "uv.lock"))
