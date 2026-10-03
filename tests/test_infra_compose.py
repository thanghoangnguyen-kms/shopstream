"""Static contract tests for infra/compose.yaml and the Postgres init scripts. No Docker.

tests/test_repo_policy.py holds the generic rules (digests, loopback ports, literal memory
limits, healthchecks). These tests pin what is specific to this stack: the service sets, the
start order, the published ports, the secret mounts and the CDC-ready init.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
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
SPIKE = {
    "probe",
    "spark-job",
    "dbt-job",
    "frankfurter-fx-init",
    "frankfurter-seed",
    "frankfurter-offline",
    "fx-load",
    "cdc-run",
}
# The one spike service that stays up (item 10's web-only Frankfurter), so it has a healthcheck.
SPIKE_LONG_RUNNING = {"frankfurter-offline"}
SPIKE_IMAGE_SERVICES = {"probe", "spark-job", "fx-load", "cdc-run"}
FRANKFURTER_SPIKE = {"frankfurter-fx-init", "frankfurter-seed", "frankfurter-offline"}
STREAMING = {"kafka-1", "kafka-2", "kafka-3", "kafka-init", "karapace", "connect", "cdc-init"}
KAFKA_NODES = ("kafka-1", "kafka-2", "kafka-3")
SPIKE_DOCKERFILE = INFRA / "spike" / "Dockerfile"
CONNECT_DOCKERFILE = INFRA / "connect" / "Dockerfile"
CDC_SQL = (INFRA / "postgres" / "cdc.sql").read_text(encoding="utf-8")
CREATE_TOPICS = (INFRA / "kafka" / "create-topics.sh").read_text(encoding="utf-8")
SINK_COMMIT = "6976e020b894f6a6777704df2b8c4458cb291ae9"
DBT_DOCKERFILE = INFRA / "dbt" / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
PYTHON_IMAGE_DIGEST = "sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b"
MIB = 2**20
W05_RESERVE = 384 * MIB
VM_BUDGET = 10 * 2**30
CAPTURED_TABLES = ["customers", "products", "orders", "order_items"]
CDC_TABLES = [*CAPTURED_TABLES, "reviews"]
# ADR-001 go criterion 8: the Colima VM is 12 GiB and one GiB stays free. Provisional until Phase 5.
LONG_RUNNING_BUDGET = 11 * 2**30


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


def test_only_the_four_profiles_hold_services_and_the_sets_are_exact() -> None:
    used = {profile for service in SERVICES.values() for profile in service.get("profiles", [])}
    assert used == {"core", "bootstrap", "spike", "streaming"}
    assert set(SERVICES) == CORE | BOOTSTRAP | SPIKE | STREAMING
    assert profile_services("core") == CORE
    assert profile_services("bootstrap") == BOOTSTRAP
    assert profile_services("spike") == SPIKE
    assert profile_services("streaming") == STREAMING
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
    return BOOTSTRAP | (SPIKE - SPIKE_LONG_RUNNING) | waited_on


def test_the_one_shots_are_the_bootstrap_and_spike_profiles_and_the_completed_dependencies() -> (
    None
):
    assert one_shots() == (
        BOOTSTRAP
        | (SPIKE - SPIKE_LONG_RUNNING)
        | {"lakekeeper-migrate", "frankfurter-init", "frankfurter-fx-init"}
        | {"kafka-init", "cdc-init"}
    )


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
        ("probe", {"lakekeeper": "service_healthy", "seaweedfs": "service_healthy"}),
        ("spark-job", {"lakekeeper": "service_healthy", "seaweedfs": "service_healthy"}),
        ("dbt-job", {"lakekeeper": "service_healthy", "seaweedfs": "service_healthy"}),
        ("frankfurter-fx-init", {}),
        ("frankfurter-seed", {"frankfurter-fx-init": "service_completed_successfully"}),
        ("frankfurter-offline", {}),
        ("fx-load", {"frankfurter-offline": "service_healthy"}),
        ("cdc-run", {"lakekeeper": "service_healthy", "seaweedfs": "service_healthy"}),
        ("kafka-1", {}),
        ("kafka-2", {}),
        ("kafka-3", {}),
        (
            "kafka-init",
            {
                "kafka-1": "service_healthy",
                "kafka-2": "service_healthy",
                "kafka-3": "service_healthy",
            },
        ),
        ("cdc-init", {"postgres": "service_healthy"}),
        ("karapace", {"kafka-init": "service_completed_successfully"}),
        (
            "connect",
            {
                "kafka-init": "service_completed_successfully",
                "cdc-init": "service_completed_successfully",
                "karapace": "service_healthy",
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
        "max_slot_wal_keep_size=4GB",
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
        return {key for key in SERVICES[name].get("environment", {}) if key in ENV_EXAMPLE}

    assert secrets_of("bootstrap") == {"SEAWEEDFS_ADMIN_KEY", "SEAWEEDFS_ADMIN_SECRET"}
    assert secrets_of("warehouse") == {"LAKEKEEPER_S3_KEY", "LAKEKEEPER_S3_SECRET"}
    assert secrets_of("probe") == {
        "LAKEKEEPER_S3_KEY",
        "LAKEKEEPER_S3_SECRET",
        "PROBE_OTHER_KEY",
        "PROBE_OTHER_SECRET",
        "SEAWEEDFS_ADMIN_KEY",
        "SEAWEEDFS_ADMIN_SECRET",
    }
    # Lakekeeper vends the storage credentials to the JVM, so the job holds no static key.
    assert secrets_of("spark-job") == set()
    assert secrets_of("dbt-job") == set()
    for name in sorted(FRANKFURTER_SPIKE | {"fx-load"}):
        assert secrets_of(name) == set(), name
    assert secrets_of("connect") == {"CDC_DB_PASSWORD"}
    assert secrets_of("cdc-init") == {"POSTGRES_PASSWORD", "CDC_DB_PASSWORD"}
    assert secrets_of("cdc-run") == {"SHOPSTREAM_DB_PASSWORD", "CDC_DB_PASSWORD", "CANARY_TOKEN"}
    for name in (*KAFKA_NODES, "kafka-init", "karapace"):
        assert secrets_of(name) == set(), name


# --- the spike one-shots ----------------------------------------------------------------------


def dockerfile_lines(path: Path = SPIKE_DOCKERFILE) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("name", sorted(SPIKE_IMAGE_SERVICES))
def test_each_spike_service_is_built_from_the_spike_dockerfile_and_locked_down(name: str) -> None:
    service = SERVICES[name]
    assert "image" not in service
    assert service["build"] == {"context": "..", "dockerfile": "infra/spike/Dockerfile"}
    assert service["read_only"] is True
    assert service["user"] == "65534:65534"
    assert [PurePosixPath(entry.split(":")[0]).parts for entry in service["tmpfs"]] == [
        ("/", "tmp")
    ]
    assert service["volumes"] == ["../scripts:/app:ro"]
    assert "ports" not in service


def test_the_dbt_job_is_built_from_the_dbt_dockerfile_and_locked_down() -> None:
    service = SERVICES["dbt-job"]
    assert "image" not in service
    assert service["build"] == {"context": "..", "dockerfile": "infra/dbt/Dockerfile"}
    assert service["working_dir"] == "/work/analytics/dbt"
    assert service["read_only"] is True
    assert service["user"] == "65534:65534"
    (entry,) = service["tmpfs"]
    mount, _, options = entry.partition(":")
    assert PurePosixPath(mount).parts == ("/", "tmp")
    assert options.split(",") == ["size=512m", "exec"]
    assert service["volumes"] == ["../analytics:/work/analytics", "../scripts:/app:ro"]
    assert "ports" not in service
    assert service["mem_limit"] == "1g"
    assert service["memswap_limit"] == "1g"


def test_the_probe_has_no_ambient_aws_configuration() -> None:
    environment = SERVICES["probe"]["environment"]
    assert environment["AWS_CONFIG_FILE"] == "/dev/null"
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    for ambient in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    ):
        assert ambient not in environment


def test_the_spark_job_limits_are_the_literal_three_gigabyte_pair() -> None:
    service = SERVICES["spark-job"]
    assert service["mem_limit"] == "3g"
    assert service["memswap_limit"] == "3g"


def test_the_spark_job_tmpfs_is_exec_because_zstd_jni_maps_a_native_library_from_it() -> None:
    (entry,) = SERVICES["spark-job"]["tmpfs"]
    assert entry.split(":")[1].split(",") == ["size=512m", "exec"]


def test_the_dockerignore_denies_everything_but_the_lock_inputs() -> None:
    lines = [
        line
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    assert lines[1:]
    assert all(line.startswith("!") for line in lines[1:])
    secrets = (
        "infra/.env",
        "infra/.generated/seaweedfs/iam.json",
        ".git/config",
        ".venv/bin/python",
    )
    for line in lines[1:]:
        assert not [path for path in secrets if fnmatch.fnmatch(path, line[1:])], line


def test_the_spike_dockerfile_pins_the_python_base_and_copies_only_lock_inputs() -> None:
    lines = dockerfile_lines()
    froms = [line for line in lines if line.startswith("FROM ")]
    assert len(froms) == 1
    assert froms[0].endswith(f"@{PYTHON_IMAGE_DIGEST}")
    assert ":3.13" in froms[0]
    allowed = {"pyproject.toml", "uv.lock", "packages/generator/pyproject.toml"}
    for line in lines:
        if line.startswith("COPY ") and "--from=" not in line:
            assert set(line.split()[1:-1]) <= allowed, line


def test_every_add_is_checksum_verified_and_the_sync_names_both_groups() -> None:
    text = SPIKE_DOCKERFILE.read_text(encoding="utf-8")
    adds = [line for line in dockerfile_lines() if line.startswith("ADD ")]
    assert len(adds) == 2
    for line in adds:
        assert re.search(r"--checksum=sha256:[0-9a-f]{64}\b", line), line
        assert "--chmod=644" in line, line
        assert re.search(
            r"https://repo1\.maven\.org/maven2/org/apache/iceberg/\S+-1\.11\.0\.jar", line
        )
    assert "uv sync --frozen --only-group spike --only-group spark" in text


def test_the_dbt_dockerfile_pins_the_base_syncs_the_lock_and_keeps_dbt_off_the_path() -> None:
    lines = dockerfile_lines(DBT_DOCKERFILE)
    froms = [line for line in lines if line.startswith("FROM ")]
    assert len(froms) == 1
    assert froms[0].endswith(f"@{PYTHON_IMAGE_DIGEST}")
    assert ":3.13" in froms[0]
    assert not [line for line in lines if line.startswith("ADD ")]
    allowed = {
        "analytics/dbt/pyproject.toml",
        "analytics/dbt/uv.lock",
        "analytics/metricflow/pyproject.toml",
        "analytics/metricflow/uv.lock",
    }
    for line in lines:
        if line.startswith("COPY ") and "--from=" not in line:
            assert set(line.split()[1:-1]) <= allowed, line
    text = DBT_DOCKERFILE.read_text(encoding="utf-8")
    assert "uv sync --frozen --project /src/dbt" in text
    assert "uv sync --frozen --project /src/mf" in text
    # Each CLI is called by absolute path: no ENV line may put a venv on PATH or name one.
    assert not [
        line
        for line in lines
        if line.startswith("ENV ") and ("/opt/dbt" in line or "/opt/mf" in line)
    ]


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


# --- item 10: the seeded volume and the cut network -------------------------------------------


def test_the_fx_offline_network_is_internal() -> None:
    assert COMPOSE["networks"]["fx_offline"]["internal"] is True
    assert set(COMPOSE["networks"]) == {"fx_offline"}


def test_only_fx_load_and_frankfurter_offline_set_networks_and_only_on_fx_offline() -> None:
    assert SERVICES["fx-load"]["networks"] == ["fx_offline"]
    assert SERVICES["frankfurter-offline"]["networks"] == {
        "fx_offline": {"aliases": ["frankfurter"]}
    }
    setting = {name for name, service in SERVICES.items() if "networks" in service}
    assert setting == {"fx-load", "frankfurter-offline"}
    for name in ("fx-load", "frankfurter-offline"):
        assert "ports" not in SERVICES[name], name


def test_the_fx_volume_is_mounted_by_the_three_frankfurter_spike_services_only() -> None:
    mounting = {name for name in SERVICES if "frankfurter-fx-data" in bind_sources(name)}
    assert mounting == FRANKFURTER_SPIKE
    assert "frankfurter-fx-data" in COMPOSE["volumes"]
    # The core frankfurter never mounts the spike volume, so one SQLite file never has two writers.
    assert bind_sources("frankfurter") == ["frankfurter-data"]
    assert bind_sources("frankfurter-init") == ["frankfurter-data"]


def test_the_frankfurter_spike_services_use_the_core_frankfurter_image() -> None:
    for name in sorted(FRANKFURTER_SPIKE):
        assert SERVICES[name]["image"] == SERVICES["frankfurter"]["image"], name


def test_the_offline_frankfurter_is_web_only_and_has_a_healthcheck() -> None:
    offline = SERVICES["frankfurter-offline"]
    assert "healthcheck" in offline
    entrypoint = " ".join(offline["entrypoint"])
    assert "puma" in entrypoint
    assert "foreman" not in entrypoint
    assert "depends_on" not in offline


def test_the_seed_is_an_online_one_shot_on_the_default_network() -> None:
    seed = SERVICES["frankfurter-seed"]
    assert "networks" not in seed
    assert "backfill[ECB]" in " ".join(seed["entrypoint"])
    assert "db:setup" in " ".join(seed["entrypoint"])


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


# --- the streaming profile --------------------------------------------------------------------


def test_no_streaming_service_publishes_a_port() -> None:
    for name in sorted(STREAMING):
        assert "ports" not in SERVICES[name], name


def test_the_worker_prefix_never_carries_an_interpolated_variable() -> None:
    # The Debezium entrypoint writes every CONNECT_* variable into a properties file and echoes
    # its value in the log, so a secret must never be given such a name.
    for name, service in SERVICES.items():
        for key, value in service.get("environment", {}).items():
            if key.startswith("CONNECT_"):
                assert "${" not in str(value), f"{name}: {key}"


def test_connect_loads_the_env_config_provider_allowlisted_for_the_one_secret() -> None:
    environment = SERVICES["connect"]["environment"]
    assert environment["CONNECT_CONFIG_PROVIDERS"] == "env"
    assert environment["CONNECT_CONFIG_PROVIDERS_ENV_CLASS"] == (
        "org.apache.kafka.common.config.provider.EnvVarConfigProvider"
    )
    assert environment["CONNECT_CONFIG_PROVIDERS_ENV_PARAM_ALLOWLIST_PATTERN"] == "CDC_DB_PASSWORD"
    assert environment["CDC_DB_PASSWORD"].endswith(":?run just up}")
    assert "EnvVarConfigProvider" in COMPOSE_TEXT


def test_connect_is_built_from_its_dockerfile_and_keeps_the_worker_variables_the_image_reads() -> (
    None
):
    connect = SERVICES["connect"]
    assert "image" not in connect
    assert connect["build"] == {"context": "..", "dockerfile": "infra/connect/Dockerfile"}
    environment = connect["environment"]
    assert environment["CONFIG_STORAGE_TOPIC"] == "connect-configs"
    assert environment["OFFSET_STORAGE_TOPIC"] == "connect-offsets"
    assert environment["STATUS_STORAGE_TOPIC"] == "connect-status"
    assert environment["OFFSET_FLUSH_INTERVAL_MS"] == "5000"
    assert environment["AWS_REGION"] == "local-01"


def test_the_kafka_nodes_and_kafka_init_share_one_image_and_a_three_way_replicated_cluster() -> (
    None
):
    images = {SERVICES[name]["image"] for name in (*KAFKA_NODES, "kafka-init")}
    assert len(images) == 1
    assert next(iter(images)).startswith("docker.io/apache/kafka:4.3.1@sha256:")
    ids = [SERVICES[name]["environment"]["KAFKA_NODE_ID"] for name in KAFKA_NODES]
    assert ids == ["1", "2", "3"]
    assert len({SERVICES[name]["environment"]["CLUSTER_ID"] for name in KAFKA_NODES}) == 1
    for name in KAFKA_NODES:
        environment = SERVICES[name]["environment"]
        assert environment["KAFKA_DEFAULT_REPLICATION_FACTOR"] == "3"
        assert environment["KAFKA_MIN_INSYNC_REPLICAS"] == "2"
        assert environment["KAFKA_AUTO_CREATE_TOPICS_ENABLE"] == "false"
        assert environment["KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR"] == "3"
        assert environment["KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR"] == "3"
        assert environment["KAFKA_ADVERTISED_LISTENERS"] == f"PLAINTEXT://{name}:19092"
        assert bind_sources(name) == [f"{name}-data"]
        assert f"{name}-data" in COMPOSE["volumes"]


def test_karapace_creates_its_schema_topic_at_replication_factor_three() -> None:
    environment = SERVICES["karapace"]["environment"]
    assert environment["KARAPACE_REPLICATION_FACTOR"] == "3"
    assert environment["KARAPACE_TOPIC_NAME"] == "_schemas"


def test_the_connect_dockerfile_pins_every_input() -> None:
    lines = dockerfile_lines(CONNECT_DOCKERFILE)
    froms = [line for line in lines if line.startswith("FROM ")]
    assert len(froms) == 3
    for line in froms:
        assert re.search(r"@sha256:[0-9a-f]{64}\b", line), line
    unpack = next(line for line in froms if line.endswith("AS unpack"))
    assert f"@{PYTHON_IMAGE_DIGEST}" in unpack
    git_adds = [
        line for line in lines if line.startswith("ADD ") and "github.com/apache/iceberg" in line
    ]
    assert len(git_adds) == 1
    assert git_adds[0].split()[1] == f"https://github.com/apache/iceberg.git#{SINK_COMMIT}"
    sums = [line for line in lines if line.startswith("ADD --checksum=sha256:")]
    assert len(sums) == 1
    assert re.match(r"ADD --checksum=sha256:[0-9a-f]{64} --chmod=644 https://", sums[0])
    assert "kafka-connect-avro-converter-8.3.2.zip" in sums[0]
    assert len([line for line in lines if line.startswith("ADD ")]) == 2
    copies = [line for line in lines if line.startswith("COPY ")]
    assert copies
    for line in copies:
        assert re.search(r"--from=(sink-build|unpack)\b", line), line


def test_cdc_sql_is_idempotent_and_takes_the_password_only_as_a_psql_variable() -> None:
    assert r"\connect shopstream" in CDC_SQL
    assert CDC_SQL.count(r"\gexec") == 2
    assert CDC_SQL.count(":'cdc_pw'") == 1
    assert "PASSWORD '" not in CDC_SQL
    assert "REPLICATION" in CDC_SQL
    assert "NOSUPERUSER" in CDC_SQL
    tables = ", ".join(CDC_TABLES)
    assert f"CREATE PUBLICATION shopstream_cdc FOR TABLE {tables}" in CDC_SQL
    assert f"GRANT SELECT ON {tables} TO cdc;" in CDC_SQL
    assert "ALTER TABLE reviews REPLICA IDENTITY FULL;" in CDC_SQL
    assert "CREATE TABLE IF NOT EXISTS reviews" in CDC_SQL
    assert not re.search(r"\b(INSERT|COPY)\b", CDC_SQL)


def test_create_topics_makes_every_topic_idempotently_at_replication_factor_three() -> None:
    assert "set -euo pipefail" in CREATE_TOPICS
    assert "--if-not-exists" in CREATE_TOPICS
    assert "--replication-factor 3" in CREATE_TOPICS
    assert "min.insync.replicas=2" in CREATE_TOPICS
    topics = [
        *(f"shopstream.public.{table}" for table in CDC_TABLES),
        "control-iceberg",
        "__debezium-heartbeat.shopstream",
        "fx.refresh",
        "connect-configs",
        "connect-offsets",
        "connect-status",
        "_schemas",
    ]
    for topic in topics:
        assert re.search(rf"^create {re.escape(topic)} \d", CREATE_TOPICS, flags=re.MULTILINE), (
            topic
        )
    for table in CDC_TABLES:
        assert f"create shopstream.public.{table} 3 cleanup.policy=delete" in CREATE_TOPICS


def test_the_long_running_services_of_core_and_streaming_fit_the_vm_budget() -> None:
    long_running = (CORE | STREAMING) - one_shots()
    total = sum(memory_bytes(SERVICES[name]["mem_limit"]) for name in long_running)
    assert total <= LONG_RUNNING_BUDGET
