"""Pins item 9's CI shape and ADR-001's no-login go branch on the dbt-core fallback.

CI runs dbt as one added step in the existing required `test` job, on the offline `ci` target,
with no dbt login, no secret and no Compose stack. These tests go red if a fifth job, a
`pull_request_target` trigger, a `secrets.` expression, a docker step, a tracked dbt_cloud.yml
or catalogs.yml, or a dbt command off the ci target or without telemetry opted out appears.
They also go red if the contract-lint step leaves the test job or the `check` recipe, or the
`contract-lint` recipe stops syncing the contracts/ project or linting every contract.
They read files only and use no network.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
JUSTFILE = REPO / "justfile"
REQUIRED_CHECKS = {"lint", "test", "secrets", "pr-title"}
TEST_JOB_RUN_STEPS = [
    "uv sync --locked",
    "uv run just test",
    "uv run just dbt-ci",
    "uv run just contract-lint",
]


def workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def load(path: Path) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def all_jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = {}
    for path in workflow_files():
        for job_id, job in load(path)["jobs"].items():
            jobs[f"{path.name}:{job_id}"] = job
    return jobs


def all_steps() -> list[dict[str, Any]]:
    return [step for job in all_jobs().values() for step in job.get("steps", [])]


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The `on` mapping; PyYAML reads the bare key `on` as the boolean True."""
    raw = workflow.get("on", workflow.get(True))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(event): None for event in raw}
    assert isinstance(raw, dict)
    mapping: dict[str, Any] = raw
    return mapping


def justfile_lines() -> list[str]:
    return JUSTFILE.read_text(encoding="utf-8").splitlines()


def recipe_body(name: str) -> list[str]:
    lines = justfile_lines()
    start = next(i for i, line in enumerate(lines) if re.match(rf"^{re.escape(name)}\b.*:", line))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if line.strip():
            body.append(line.strip())
    return body


def git_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return result.stdout.splitlines()


def test_the_required_checks_stay_exactly_four() -> None:
    names = {job.get("name", key.split(":", 1)[1]) for key, job in all_jobs().items()}
    assert names == REQUIRED_CHECKS
    assert len(all_jobs()) == len(REQUIRED_CHECKS)


def test_no_workflow_runs_on_pull_request_target() -> None:
    for path in workflow_files():
        assert "pull_request_target" not in triggers(load(path)), path.name


def test_no_workflow_references_a_secret_or_a_dbt_cloud_variable() -> None:
    for path in workflow_files():
        assert "secrets." not in path.read_text(encoding="utf-8"), path.name
    for step in all_steps():
        assert not any(str(key).upper().startswith("DBT_CLOUD") for key in step.get("env", {}))


def test_the_test_job_runs_the_dbt_and_contract_recipes_after_the_tests() -> None:
    steps = load(WORKFLOWS / "ci.yml")["jobs"]["test"]["steps"]
    assert [step["run"] for step in steps if "run" in step] == TEST_JOB_RUN_STEPS


def test_no_step_starts_docker_or_the_compose_stack() -> None:
    for step in all_steps():
        text = f"{step.get('run', '')} {step.get('uses', '')}".lower()
        assert "docker" not in text
        assert "compose" not in text


def test_check_lists_the_dbt_recipe_after_test() -> None:
    check = next(line for line in justfile_lines() if line.startswith("check:"))
    dependencies = check.removeprefix("check:").split()
    assert dependencies.index("dbt-ci") == dependencies.index("test") + 1


def test_check_lists_the_contract_recipe_after_the_dbt_recipe() -> None:
    check = next(line for line in justfile_lines() if line.startswith("check:"))
    dependencies = check.removeprefix("check:").split()
    assert dependencies.index("contract-lint") == dependencies.index("dbt-ci") + 1


def test_the_contract_recipe_lints_every_contract_from_the_contracts_project() -> None:
    body = recipe_body("contract-lint")
    assert body[0] == "uv sync --locked --project contracts"
    loop = body[1]
    assert "contracts/*/*.odcs.yaml" in loop
    assert "uv run --frozen --project contracts datacontract lint" in loop


def test_every_dbt_command_in_the_recipe_is_offline_and_opted_out_of_telemetry() -> None:
    dbt_lines = [line for line in recipe_body("dbt-ci") if re.search(r"\bdbt (build|docs)\b", line)]
    assert len(dbt_lines) == 2
    for line in dbt_lines:
        assert line.startswith("DO_NOT_TRACK=1 "), line
        assert "--target ci" in line, line
    docs = next(line for line in dbt_lines if "docs generate" in line)
    assert "--static" in docs


def test_the_recipe_runs_no_dbt_login_or_lint() -> None:
    for line in recipe_body("dbt-ci"):
        assert not re.search(r"\bdbt (login|lint)\b", line), line


@pytest.mark.parametrize("name", ["dbt_cloud.yml", "catalogs.yml"])
def test_no_dbt_account_or_catalog_file_is_tracked(name: str) -> None:
    assert [path for path in git_files() if Path(path).name == name] == []
