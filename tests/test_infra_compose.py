"""Static contract tests for infra/compose.yaml and the Postgres init scripts. No Docker.

tests/test_repo_policy.py holds the generic rules (digests, loopback ports, literal memory
limits, healthchecks). These tests pin what is specific to this stack: the service sets, the
start order, the published ports, the secret mounts and the CDC-ready init.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from dotenv_lite import parse_dotenv

REPO = Path(__file__).resolve().parents[1]
INFRA = REPO / "infra"
COMPOSE_TEXT = (INFRA / "compose.yaml").read_text(encoding="utf-8")
COMPOSE: dict[str, Any] = yaml.safe_load(COMPOSE_TEXT)
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
INIT_DIR = INFRA / "postgres" / "init"
SQL = (INIT_DIR / "20-shopstream.sql").read_text(encoding="utf-8")
SQL_CODE = "\n".join(line for line in SQL.splitlines() if not line.lstrip().startswith("--"))
SHELL_INIT = (INIT_DIR / "10-databases.sh").read_text(encoding="utf-8")
ENV_EXAMPLE = parse_dotenv((INFRA / ".env.example").read_text(encoding="utf-8"))

CORE = {
    "postgres",
    "lakekeeper-migrate",
    "lakekeeper",
    "seaweedfs",
    "frankfurter-init",
    "frankfurter",
}
BOOTSTRAP = {"bootstrap", "warehouse"}
PYTHON_IMAGE_DIGEST = "sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b"
MIB = 2**20
W05_RESERVE = 384 * MIB
VM_BUDGET = 10 * 2**30
CAPTURED_TABLES = ["customers", "products", "orders", "order_items"]


def memory_bytes(value: str | int) -> int:
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"(\d+)([bkmg]?)b?", value.lower())
    assert match, value
    unit = {"": 1, "b": 1, "k": 1024, "m": MIB, "g": 2**30}[match.group(2)]
    return int(match.group(1)) * unit


def profile_services(profile: str) -> set[str]:
    return {name for name, service in SERVICES.items() if profile in service.get("profiles", [])}


def conditions(name: str) -> dict[str, str]:
    depends: dict[str, dict[str, str]] = SERVICES[name].get("depends_on", {})
    return {dependency: spec["condition"] for dependency, spec in depends.items()}


def bind_sources(name: str) -> list[str]:
    return [volume.split(":")[0] for volume in SERVICES[name].get("volumes", [])]


def table_bodies() -> dict[str, str]:
    found = re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", SQL, flags=re.DOTALL)
    return dict(found)


# --- shape ------------------------------------------------------------------------------------


def test_the_project_is_named_and_uses_no_deploy_block() -> None:
    assert COMPOSE["name"] == "shopstream"
    assert not [name for name, service in SERVICES.items() if "deploy" in service]


def test_only_core_and_bootstrap_hold_services_and_the_sets_are_exact() -> None:
    used = {profile for service in SERVICES.values() for profile in service.get("profiles", [])}
    assert used == {"core", "bootstrap"}
    assert set(SERVICES) == CORE | BOOTSTRAP
    assert profile_services("core") == CORE
    assert profile_services("bootstrap") == BOOTSTRAP
    assert all(len(service["profiles"]) == 1 for service in SERVICES.values())


def test_published_ports_are_exactly_the_four_loopback_bindings() -> None:
    published = sorted(port for service in SERVICES.values() for port in service.get("ports", []))
    assert published == [
        "127.0.0.1:5432:5432",
        "127.0.0.1:8090:8080",
        "127.0.0.1:8181:8181",
        "127.0.0.1:8333:8333",
    ]
    hosts = [":".join(port.split(":")[:2]) for port in published]
    assert len(set(hosts)) == len(hosts)


def test_the_seaweedfs_master_and_filer_are_not_published() -> None:
    assert SERVICES["seaweedfs"]["ports"] == ["127.0.0.1:8333:8333"]


def test_every_service_has_equal_literal_memory_and_swap_limits() -> None:
    for name, service in SERVICES.items():
        assert memory_bytes(service["mem_limit"]) == memory_bytes(service["memswap_limit"]), name


def test_the_starting_memory_limits() -> None:
    limits = {name: memory_bytes(service["mem_limit"]) for name, service in SERVICES.items()}
    assert limits["postgres"] == 512 * MIB
    assert limits["seaweedfs"] == 768 * MIB
    assert limits["lakekeeper"] == 256 * MIB
    # Plan 01-04 may raise Frankfurter's limit from the measured peak, never lower it.
    assert limits["frankfurter"] >= 192 * MIB


def test_core_limits_plus_the_w05_reserve_fit_the_vm_budget() -> None:
    core = sum(memory_bytes(SERVICES[name]["mem_limit"]) for name in CORE)
    assert core + W05_RESERVE < VM_BUDGET


# --- one-shots and healthchecks ---------------------------------------------------------------


def one_shots() -> set[str]:
    waited_on = {
        dependency
        for service in SERVICES.values()
        for dependency, spec in service.get("depends_on", {}).items()
        if spec["condition"] == "service_completed_successfully"
    }
    return BOOTSTRAP | waited_on


def test_the_one_shots_are_the_bootstrap_profile_and_the_completed_dependencies() -> None:
    assert one_shots() == BOOTSTRAP | {"lakekeeper-migrate", "frankfurter-init"}


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_one_shots_never_restart_and_have_no_healthcheck_and_everything_else_has_one(
    name: str,
) -> None:
    service = SERVICES[name]
    assert service["restart"] == "no"
    if name in one_shots():
        assert "healthcheck" not in service
    else:
        assert "healthcheck" in service


# --- start order ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("postgres", {}),
        ("seaweedfs", {}),
        ("lakekeeper-migrate", {"postgres": "service_healthy"}),
        (
            "lakekeeper",
            {"lakekeeper-migrate": "service_completed_successfully", "postgres": "service_healthy"},
        ),
        ("frankfurter-init", {}),
        ("frankfurter", {"frankfurter-init": "service_completed_successfully"}),
        ("bootstrap", {"lakekeeper": "service_healthy", "seaweedfs": "service_healthy"}),
        (
            "warehouse",
            {
                "bootstrap": "service_completed_successfully",
                "lakekeeper": "service_healthy",
                "seaweedfs": "service_healthy",
            },
        ),
    ],
)
def test_start_order_is_enforced_by_depends_on_conditions(
    name: str, expected: dict[str, str]
) -> None:
    assert conditions(name) == expected


# --- postgres ---------------------------------------------------------------------------------


def test_postgres_starts_cdc_ready_and_probes_over_tcp() -> None:
    postgres = SERVICES["postgres"]
    flags = " ".join(postgres["command"])
    for setting in (
        "wal_level=logical",
        "track_commit_timestamp=on",
        "max_replication_slots=4",
        "max_wal_senders=4",
    ):
        assert f"-c {setting}" in flags
    assert "-h 127.0.0.1" in " ".join(postgres["healthcheck"]["test"])


def test_the_init_scripts_run_in_lexical_order_roles_before_tables() -> None:
    names = sorted(path.name for path in INIT_DIR.iterdir())
    assert names == ["10-databases.sh", "20-shopstream.sql"]
    assert "./postgres/init:/docker-entrypoint-initdb.d:ro" in SERVICES["postgres"]["volumes"]


def test_the_database_script_is_executable_in_the_worktree_and_the_index() -> None:
    assert os.access(INIT_DIR / "10-databases.sh", os.X_OK)
    staged = subprocess.run(
        ["git", "ls-files", "-s", "--", "infra/postgres/init/10-databases.sh"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout
    assert staged.startswith("100755 "), staged


def test_the_database_script_creates_the_databases_and_lakekeeper_extensions() -> None:
    assert "ON_ERROR_STOP=1" in SHELL_INIT
    assert "set -euo pipefail" in SHELL_INIT
    for database in ("lakekeeper", "airflow", "shopstream"):
        assert re.search(rf"CREATE DATABASE {database} OWNER {database};", SHELL_INIT), database
    for extension in ("uuid-ossp", "pgcrypto", "pg_trgm", "btree_gin", "btree_gist"):
        assert extension in SHELL_INIT
    # Role passwords go in as psql variables, never spliced into the SQL text.
    assert SHELL_INIT.count(":'") == 3
    assert "PASSWORD '" not in SHELL_INIT


def test_the_captured_tables_are_created_in_the_shopstream_database() -> None:
    assert SQL.index(r"\connect shopstream") < SQL.index("CREATE TABLE")
    assert list(table_bodies()) == CAPTURED_TABLES


@pytest.mark.parametrize("table", CAPTURED_TABLES)
def test_each_captured_table_has_a_primary_key_and_a_required_updated_at(table: str) -> None:
    body = table_bodies()[table]
    assert "PRIMARY KEY" in body
    assert re.search(r"\bupdated_at timestamptz NOT NULL\b", body)
    assert f"ALTER TABLE {table} REPLICA IDENTITY FULL;" in SQL


def test_the_captured_tables_have_no_foreign_keys_and_no_rows() -> None:
    assert not re.search(r"REFERENCES|FOREIGN KEY", SQL_CODE, flags=re.IGNORECASE)
    assert not re.search(r"\b(INSERT|COPY)\b", SQL_CODE, flags=re.IGNORECASE)
    assert "product_id bigint NOT NULL," in table_bodies()["order_items"]


# --- lakekeeper -------------------------------------------------------------------------------


def test_lakekeeper_runs_allowall_and_never_logs_bodies_or_skips_validation() -> None:
    for name in ("lakekeeper-migrate", "lakekeeper"):
        assert SERVICES[name]["environment"]["LAKEKEEPER__AUTHZ_BACKEND"] == "allowall"
    for forbidden in ("SKIP_STORAGE_VALIDATION", "LOG_REQUEST_BODIES", "REQUEST_BODY"):
        assert forbidden not in COMPOSE_TEXT


# --- seaweedfs and the identity file ----------------------------------------------------------


def test_seaweedfs_mounts_the_identity_file_read_only_and_reads_it_twice() -> None:
    seaweedfs = SERVICES["seaweedfs"]
    assert "./.generated/seaweedfs/iam.json:/etc/seaweedfs/iam.json:ro" in seaweedfs["volumes"]
    assert "-s3.config=/etc/seaweedfs/iam.json" in seaweedfs["command"]
    assert "-s3.iam.config=/etc/seaweedfs/iam.json" in seaweedfs["command"]
    assert not [flag for flag in seaweedfs["command"] if "-bucket" in flag]


def test_no_other_service_mounts_anything_from_the_generated_directory() -> None:
    # On Colima's virtiofs any container uid can read a 0600 host file, so the mount list is
    # the container-side guard for the identity file.
    mounting = {name for name in SERVICES if any(".generated" in s for s in bind_sources(name))}
    assert mounting == {"seaweedfs"}


# --- the one-shots ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(BOOTSTRAP))
def test_the_one_shots_run_locked_down_from_the_pinned_python_image(name: str) -> None:
    service = SERVICES[name]
    assert service["image"].endswith(f"@{PYTHON_IMAGE_DIGEST}")
    assert ":3.13" in service["image"]
    assert service["read_only"] is True
    assert service["user"] == "65534:65534"
    assert service["volumes"] == [
        "../scripts/lakekeeper_bootstrap.py:/app/lakekeeper_bootstrap.py:ro"
    ]


def test_each_one_shot_sees_only_its_own_key_pair() -> None:
    def secrets_of(name: str) -> set[str]:
        return {key for key in SERVICES[name]["environment"] if key in ENV_EXAMPLE}

    assert secrets_of("bootstrap") == {"SEAWEEDFS_ADMIN_KEY", "SEAWEEDFS_ADMIN_SECRET"}
    assert secrets_of("warehouse") == {"LAKEKEEPER_S3_KEY", "LAKEKEEPER_S3_SECRET"}


# --- frankfurter ------------------------------------------------------------------------------


def test_frankfurter_is_sized_for_its_memory_limit() -> None:
    frankfurter = SERVICES["frankfurter"]
    assert frankfurter["environment"]["WORKER_PROCESSES"] == "1"
    assert frankfurter["environment"]["MAX_THREADS"] == "3"
    assert frankfurter["init"] is True
    assert frankfurter["ports"] == ["127.0.0.1:8090:8080"]


def test_the_frankfurter_volume_is_chowned_before_the_service_starts() -> None:
    init = SERVICES["frankfurter-init"]
    assert init["user"] == "0:0"
    assert init["entrypoint"] == ["chown", "-R", "1000:1000", "/app/data"]
    assert bind_sources("frankfurter-init") == bind_sources("frankfurter") == ["frankfurter-data"]


# --- secrets ----------------------------------------------------------------------------------


def test_every_key_in_the_env_example_is_empty() -> None:
    assert ENV_EXAMPLE
    assert not {key: value for key, value in ENV_EXAMPLE.items() if value}
    assert "CANARY_TOKEN" in ENV_EXAMPLE


def test_every_interpolated_variable_is_declared_in_the_env_example() -> None:
    used = set(re.findall(r"\$\{(\w+)", COMPOSE_TEXT))
    assert used
    assert used <= set(ENV_EXAMPLE), used - set(ENV_EXAMPLE)


def test_every_secret_interpolation_fails_loudly_when_blank() -> None:
    plain = re.findall(r"\$\{(\w+)\}", COMPOSE_TEXT)
    assert plain == []
