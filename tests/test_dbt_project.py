"""Pins item 3's fallback runtime and the one-project layout of analytics/dbt.

ADR-001 fixes dbt-core 1.x with dbt-duckdb on DuckDB 1.5.5 as item 3's fallback, and this
repo promoted it to the only build runtime. These tests are the invariant behind that decision:
they go red if the dbt 2 pin, a catalogs.yml, the wrapper directory or the catalog-integration
config key comes back. They read files only and import no dbt.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
DBT = REPO / "analytics" / "dbt"
# dbt-adapters reads this model config key as a catalog-integration name and stops with
# "Catalog not found" before any SQL runs, so models must use `iceberg_catalog` instead.
FORBIDDEN_CONFIG_KEY = "catalog_name"
CREDENTIAL_KEYS = {"password", "token", "secret", "secrets"}


def load_yaml(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((DBT / name).read_text(encoding="utf-8"))
    return loaded


def tracked(pathspec: str) -> list[str]:
    """Files git tracks or would track: the index plus untracked files that are not ignored."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", pathspec],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def key_names(node: object) -> set[str]:
    """Every mapping key at any depth, lower-cased."""
    if isinstance(node, dict):
        found = {str(key).lower() for key in node}
        for value in node.values():
            found |= key_names(value)
        return found
    if isinstance(node, list):
        found = set()
        for item in node:
            found |= key_names(item)
        return found
    return set()


def test_the_dbt_project_pins_exactly_the_fallback_runtime() -> None:
    project = tomllib.loads((DBT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    assert set(dependencies) == {"dbt-core==1.12.5", "dbt-duckdb==1.11.0", "duckdb==1.5.5"}
    assert len(dependencies) == 3
    assert not [dep for dep in dependencies if dep.startswith("dbt==")]


def test_no_catalogs_yml_and_no_wrapper_project_are_tracked() -> None:
    files = tracked("analytics")
    assert files, "git ls-files lists nothing under analytics"
    assert not [path for path in files if Path(path).name == "catalogs.yml"]
    assert not [path for path in files if path.startswith("analytics/dbt/lakekeeper/")]


def test_no_model_or_config_uses_the_catalog_integration_config_key() -> None:
    sources = [path for path in tracked("analytics/dbt") if path.endswith((".sql", ".yml"))]
    assert sources, "no tracked .sql or .yml under analytics/dbt"
    offenders = [
        path
        for path in sources
        if FORBIDDEN_CONFIG_KEY in (REPO / path).read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_profile_has_targets_lk_and_ci_and_lk_attaches_the_catalog_it_names() -> None:
    profiles = load_yaml("profiles.yml")
    assert set(profiles) == {"shopstream"}
    profile = profiles["shopstream"]
    assert profile["target"] == "lk"
    assert set(profile["outputs"]) == {"lk", "ci"}
    lk = profile["outputs"]["lk"]
    (attach,) = lk["attach"]
    assert attach["type"] == "iceberg"
    assert lk["database"] == attach["alias"]
    assert lk["database"] == load_yaml("dbt_project.yml")["vars"]["catalog"]
    ci = profile["outputs"]["ci"]
    assert ci["path"] == ":memory:"
    assert "attach" not in ci
    assert "extensions" not in ci


def test_the_project_sends_no_telemetry_and_keeps_seeds_in_the_session() -> None:
    project = load_yaml("dbt_project.yml")
    assert project["name"] == "shopstream"
    assert project["profile"] == "shopstream"
    assert project["flags"]["send_anonymous_usage_stats"] is False
    assert project["seeds"]["+database"] == "memory"


def test_the_profile_and_the_project_hold_no_credential_keys() -> None:
    for name in ("profiles.yml", "dbt_project.yml"):
        assert key_names(load_yaml(name)) & CREDENTIAL_KEYS == set(), name
