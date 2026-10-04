"""Pins the dependency cooldowns that must land before the Renovate app is installed.

uv excludes releases newer than 3 days (`[tool.uv] exclude-newer`), recorded in uv.lock as a
span so `uv lock --check` stays stable from day to day, and every pinned dependency-group entry
must still be locked at its pin. Renovate holds every update for 3 days (`minimumReleaseAge`), but GHCR and Quay return no
release timestamps, so without a `timestamp-optional` rule their updates would be held forever.
These tests go red if that rule is missing, carries another value or datasource, is widened past
GHCR and Quay, stops matching a GHCR or Quay image tracked in `infra/`, or if the top-level
3-day age is removed. They read files only and use no network.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
RENOVATE = REPO / "renovate.json"
COOLDOWN_PATTERNS = ("ghcr.io/**", "quay.io/**")
BEHAVIOUR = "timestamp-optional"
IMAGE = re.compile(r"(?:ghcr|quay)\.io/[a-z0-9._/-]+")
PYPROJECT = REPO / "pyproject.toml"
LOCK = REPO / "uv.lock"
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^;\s]+)$")


def tracked_infra_images() -> list[str]:
    """Every GHCR or Quay image named in a git-tracked file under infra/."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", "infra"],
        capture_output=True,
        check=True,
        cwd=REPO,
    ).stdout.decode("utf-8")
    images: set[str] = set()
    for name in filter(None, listing.split("\0")):
        text = (REPO / name).read_text(encoding="utf-8")
        images.update(match.rstrip("/.") for match in IMAGE.findall(text))
    return sorted(images)


def cooldown_violations(config: dict[str, Any], images: list[str]) -> list[str]:
    """What is wrong with the config's release-age rules, one message per problem."""
    problems: list[str] = []
    if config.get("minimumReleaseAge") != "3 days":
        problems.append("minimumReleaseAge must stay 3 days")
    rules = [r for r in config.get("packageRules", []) if "minimumReleaseAgeBehaviour" in r]
    if not rules:
        problems.append("no packageRules entry sets minimumReleaseAgeBehaviour")
        return problems
    if len(rules) > 1:
        problems.append("more than one rule sets it")
        return problems
    rule = rules[0]
    if rule["minimumReleaseAgeBehaviour"] != BEHAVIOUR:
        problems.append(f"minimumReleaseAgeBehaviour must be {BEHAVIOUR}")
    if rule.get("matchDatasources") != ["docker"]:
        problems.append("matchDatasources must be [docker]")
    patterns = rule.get("matchPackageNames", [])
    if set(patterns) != set(COOLDOWN_PATTERNS):
        problems.append("the rule widens timestamp-optional beyond GHCR and Quay")
    problems.extend(
        f"{image} is not matched"
        for image in images
        if not any(fnmatch.fnmatchcase(image, pattern) for pattern in patterns)
    )
    if not str(rule.get("description", "")).strip():
        problems.append("the rule needs a description")
    return problems


def valid_config() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RENOVATE.read_text(encoding="utf-8"))
    return loaded


def reference_config() -> dict[str, Any]:
    """A known-good config, independent of the real file, to mutate."""
    return {
        "minimumReleaseAge": "3 days",
        "packageRules": [
            {
                "description": "GHCR and Quay return no release timestamps",
                "matchDatasources": ["docker"],
                "matchPackageNames": list(COOLDOWN_PATTERNS),
                "minimumReleaseAgeBehaviour": BEHAVIOUR,
            }
        ],
    }


def _rule(config: dict[str, Any]) -> dict[str, Any]:
    rule: dict[str, Any] = config["packageRules"][0]
    return rule


def _drop_rules(config: dict[str, Any]) -> None:
    del config["packageRules"]


def _bogus_value(config: dict[str, Any]) -> None:
    _rule(config)["minimumReleaseAgeBehaviour"] = "bogus"


def _npm_datasource(config: dict[str, Any]) -> None:
    _rule(config)["matchDatasources"] = ["npm"]


def _star_pattern(config: dict[str, Any]) -> None:
    _rule(config)["matchPackageNames"].append("*")


def _dockerhub_pattern(config: dict[str, Any]) -> None:
    _rule(config)["matchPackageNames"].append("docker.io/**")


def _drop_quay(config: dict[str, Any]) -> None:
    _rule(config)["matchPackageNames"].remove("quay.io/**")


def _drop_age(config: dict[str, Any]) -> None:
    del config["minimumReleaseAge"]


def _drop_description(config: dict[str, Any]) -> None:
    del _rule(config)["description"]


MUTATIONS: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("packageRules removed", _drop_rules, "no packageRules entry"),
    ("value bogus", _bogus_value, "must be timestamp-optional"),
    ("datasource npm", _npm_datasource, "matchDatasources must be"),
    ("star pattern added", _star_pattern, "widens"),
    ("docker.io pattern added", _dockerhub_pattern, "widens"),
    ("quay pattern dropped", _drop_quay, "quay.io/lakekeeper/catalog is not matched"),
    ("top-level age removed", _drop_age, "minimumReleaseAge must stay 3 days"),
    ("description removed", _drop_description, "description"),
]

SAMPLE_IMAGES = ["quay.io/lakekeeper/catalog", "ghcr.io/aiven-open/karapace"]


def test_renovate_cooldown_rule_covers_ghcr_and_quay() -> None:
    assert cooldown_violations(valid_config(), tracked_infra_images()) == []


def test_tracked_infra_has_ghcr_and_quay_images() -> None:
    images = tracked_infra_images()
    for expected in (
        "quay.io/lakekeeper/catalog",
        "quay.io/debezium/connect",
        "ghcr.io/aiven-open/karapace",
        "ghcr.io/astral-sh/uv",
    ):
        assert expected in images


def test_reference_config_is_clean() -> None:
    assert cooldown_violations(reference_config(), SAMPLE_IMAGES) == []


@pytest.mark.parametrize(
    ("label", "mutate", "expected"), MUTATIONS, ids=[label for label, _, _ in MUTATIONS]
)
def test_cooldown_check_goes_red_on_mutation(
    label: str, mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    config = copy.deepcopy(reference_config())
    mutate(config)
    problems = cooldown_violations(config, SAMPLE_IMAGES)
    assert any(expected in problem for problem in problems), (label, problems)


def normalize(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def group_pins(pyproject: dict[str, Any]) -> list[tuple[str, str]]:
    """Every dependency-group entry as (normalized name, version); a non-pin raises."""
    pins: list[tuple[str, str]] = []
    for group, entries in pyproject.get("dependency-groups", {}).items():
        for entry in entries:
            match = PIN.match(entry)
            assert match, f"group {group} entry {entry!r} is not an exact == pin"
            pins.append((normalize(match.group(1)), match.group(2)))
    return pins


def locked_versions(lock: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (normalize(package["name"]), package["version"])
        for package in lock.get("package", [])
        if "version" in package
    }


def unlocked_pins(pins: list[tuple[str, str]], locked: set[tuple[str, str]]) -> list[str]:
    return [f"{name}=={version}" for name, version in pins if (name, version) not in locked]


def test_uv_excludes_releases_newer_than_three_days() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert pyproject["tool"]["uv"].get("exclude-newer") == "3 days"


def test_the_lock_records_the_three_day_span() -> None:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    assert lock.get("options", {}).get("exclude-newer-span") == "P3D"


def test_every_pinned_group_dependency_is_locked_at_its_pin() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    pins = group_pins(pyproject)
    assert {"boto3", "duckdb", "pyspark", "pyiceberg", "polars"} <= {name for name, _ in pins}
    assert unlocked_pins(pins, locked_versions(lock)) == []


def test_the_pins_guard_goes_red_on_a_mutated_pin() -> None:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    pins = [*group_pins(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))), ("duckdb", "9.9.9")]
    assert unlocked_pins(pins, locked_versions(lock)) == ["duckdb==9.9.9"]
