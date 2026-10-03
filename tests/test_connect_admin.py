"""Unit tests for scripts/connect_admin.py. No network: http_request is replaced."""

from __future__ import annotations

import io
import json
import re
import secrets
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import connect_admin as ca
import pytest

PLACEHOLDER_ONLY = re.compile(r"^\$\{env:[A-Z_]+\}$")


@pytest.mark.parametrize(
    "url",
    ["http://connect:8083/connectors", "http://karapace:8081/subjects"],
)
def test_check_url_allows_connect_and_karapace(url: str) -> None:
    ca.check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://connect:8083/connectors",
        "http://evil.example/x",
        "http://connect:9999/x",
        "http://lakekeeper:8181/catalog/v1/config",
        "http://connect:8083@evil.example/x",
        "http://connect:8083.evil.example/x",
        "ftp://connect:8083/x",
        "connect:8083/x",
    ],
)
def test_check_url_refuses_everything_else_including_the_lakekeeper_origin(url: str) -> None:
    with pytest.raises(ValueError, match="allowed"):
        ca.check_url(url)


def test_the_allowlist_is_exactly_connect_and_karapace() -> None:
    assert {"http://connect:8083", "http://karapace:8081"} == ca.ALLOWED_ORIGINS


def test_http_request_refuses_a_bad_url_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("network call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    with pytest.raises(ValueError, match="allowed"):
        ca.http_request("GET", "https://connect:8083/x", {}, None)


def test_http_request_returns_the_status_of_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "http://connect:8083/x", 409, "conflict", Message(), BytesIO(b"payload")
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert ca.http_request("PUT", "http://connect:8083/x", {}, b"{}") == (409, b"payload")


# --- the connector configs ----------------------------------------------------------------------


def test_the_debezium_password_is_exactly_the_placeholder() -> None:
    config = ca.debezium_config()
    assert config["database.password"] == "${env:CDC_DB_PASSWORD}"
    for key, value in config.items():
        if key.endswith("password"):
            assert PLACEHOLDER_ONLY.match(value), key


def test_the_debezium_config_captures_the_five_public_tables_with_avro_and_pgoutput() -> None:
    config = ca.debezium_config()
    assert config["table.include.list"] == (
        "public.customers,public.products,public.orders,public.order_items,public.reviews"
    )
    assert config["snapshot.mode"] == "initial"
    assert config["tombstones.on.delete"] == "true"
    assert config["tasks.max"] == "1"
    assert config["plugin.name"] == "pgoutput"
    assert config["publication.autocreate.mode"] == "disabled"
    assert config["publication.name"] == "shopstream_cdc"
    assert config["slot.name"] == "shopstream_dbz"
    assert config["heartbeat.interval.ms"] == "10000"
    for side in ("key", "value"):
        assert config[f"{side}.converter"] == "io.confluent.connect.avro.AvroConverter"
        assert config[f"{side}.converter.schema.registry.url"] == "http://karapace:8081"


def test_every_config_value_is_a_string() -> None:
    for config in (ca.debezium_config(), ca.sink_config(), ca.sink_config(None)):
        assert all(isinstance(value, str) for value in config.values())


def test_the_sink_routes_each_topic_to_its_bronze_table() -> None:
    config = ca.sink_config()
    assert config["topics"] == ",".join(f"shopstream.public.{table}" for table in ca.TABLES)
    assert config["topics"].split(",")[0] == "shopstream.public.customers"
    assert config["iceberg.tables"] == ",".join(f"bronze.{table}" for table in ca.TABLES)
    assert config["iceberg.tables.route-field"] == "_kafka_metadata_topic"
    routes = {k: v for k, v in config.items() if k.endswith(".route-regex")}
    assert len(routes) == 5
    for table in ca.TABLES:
        assert routes[f"iceberg.table.bronze.{table}.route-regex"] == (
            rf"shopstream\.public\.{table}"
        )


def test_the_sink_writes_v2_tables_to_the_audit_branch_on_one_task() -> None:
    config = ca.sink_config()
    assert config["iceberg.tables.default-commit-branch"] == "audit"
    assert "iceberg.tables.default-commit-branch" not in ca.sink_config(None)
    assert config["iceberg.tables.auto-create-props.format-version"] == "2"
    assert config["tasks.max"] == "1"
    assert config["iceberg.control.topic"] == "control-iceberg"
    assert config["iceberg.control.commit.interval-ms"] == "15000"
    assert config["iceberg.catalog.uri"] == "http://lakekeeper:8181/catalog"
    assert config["iceberg.catalog.header.X-Iceberg-Access-Delegation"] == "vended-credentials"


def test_the_sink_runs_only_the_kafka_metadata_transform() -> None:
    config = ca.sink_config()
    assert config["transforms"] == "kafkaMeta"
    assert config["transforms.kafkaMeta.type"] == (
        "org.apache.iceberg.connect.transforms.KafkaMetadataTransform"
    )
    transform_keys = [k for k in config if k.startswith("transforms")]
    assert sorted(transform_keys) == ["transforms", "transforms.kafkaMeta.type"]
    assert not [v for v in config.values() if "Debezium" in v]


def test_no_config_names_the_real_secret_or_a_password_field_other_than_the_placeholder() -> None:
    for config in (ca.debezium_config(), ca.sink_config()):
        for key, value in config.items():
            if "password" in key or "secret" in key:
                assert PLACEHOLDER_ONLY.match(value), key


# --- the clickstream sink -----------------------------------------------------------------------


def test_the_clickstream_sink_shares_the_cdc_sinks_transform_and_catalog_lines() -> None:
    cdc = ca.sink_config()
    clicks = ca.clickstream_sink_config()
    cdc_transform = {k: v for k, v in cdc.items() if k.startswith("transforms")}
    assert cdc_transform
    assert {k: v for k, v in clicks.items() if k.startswith("transforms")} == cdc_transform
    cdc_catalog = {k: v for k, v in cdc.items() if k.startswith("iceberg.catalog")}
    assert cdc_catalog
    assert {k: v for k, v in clicks.items() if k.startswith("iceberg.catalog")} == cdc_catalog
    for key in ("key.converter", "value.converter"):
        assert clicks[key] == cdc[key]
        assert clicks[f"{key}.schema.registry.url"] == cdc[f"{key}.schema.registry.url"]


def test_the_clickstream_sink_writes_v2_to_bronze_clickstream_every_60_seconds() -> None:
    config = ca.clickstream_sink_config()
    assert config["topics"] == "clickstream"
    assert config["iceberg.tables"] == "bronze.clickstream"
    assert config["iceberg.tables.auto-create-enabled"] == "true"
    assert config["iceberg.tables.auto-create-props.format-version"] == "2"
    assert config["iceberg.control.topic"] == "control-iceberg-clicks"
    assert config["iceberg.control.commit.interval-ms"] == "60000"
    assert config["tasks.max"] == "1"
    assert "iceberg.tables.default-commit-branch" not in config
    assert ca.clickstream_sink_config(branch="audit")["iceberg.tables.default-commit-branch"] == (
        "audit"
    )
    assert "iceberg.tables.route-field" not in config


def test_the_clickstream_sink_sends_bad_records_to_a_dlq_and_logs_no_record() -> None:
    config = ca.clickstream_sink_config()
    assert config["errors.tolerance"] == "all"
    assert config["errors.deadletterqueue.topic.name"] == "clickstream.dlq"
    assert config["errors.deadletterqueue.topic.replication.factor"] == "3"
    assert config["errors.deadletterqueue.context.headers.enable"] == "true"
    assert config["errors.log.enable"] == "true"
    assert config["errors.log.include.messages"] == "false"
    one = ca.clickstream_sink_config(dlq_replication_factor=1)
    assert one["errors.deadletterqueue.topic.replication.factor"] == "1"


@pytest.mark.parametrize("tasks_max", [0, 7, -1])
def test_the_clickstream_sink_refuses_tasks_max_outside_1_to_6(tasks_max: int) -> None:
    with pytest.raises(ValueError, match="tasks_max"):
        ca.clickstream_sink_config(tasks_max=tasks_max)


def test_the_clickstream_sink_accepts_one_to_six_tasks_and_allowlisted_overrides() -> None:
    assert ca.clickstream_sink_config(tasks_max=6)["tasks.max"] == "6"
    config = ca.clickstream_sink_config(overrides={"consumer.override.max.poll.records": "5000"})
    assert config["consumer.override.max.poll.records"] == "5000"
    assert all(isinstance(value, str) for value in config.values())


def test_the_clickstream_sink_refuses_an_override_outside_the_allowlist() -> None:
    with pytest.raises(ValueError, match="override not allowed"):
        ca.clickstream_sink_config(overrides={"consumer.override.group.id": "x"})
    with pytest.raises(ValueError, match="override not allowed"):
        ca.clickstream_sink_config(overrides={"iceberg.catalog.uri": "http://elsewhere"})


def test_no_clickstream_config_key_is_named_like_a_password_or_secret() -> None:
    for key in ca.clickstream_sink_config():
        assert "password" not in key
        assert "secret" not in key


def test_config_for_resolves_the_clickstream_sink_by_name() -> None:
    assert ca._config_for(ca.CLICKSTREAM_SINK_NAME) == ca.clickstream_sink_config()
    assert ca._config_for(ca.SINK_NAME) == ca.sink_config()


def test_register_clickstream_puts_one_connector_and_reports_the_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Script(
        {
            ("PUT", "/connectors/clickstream-sink/config"): [(201, b"{}")],
            ("GET", "/connectors/clickstream-sink/status"): [running("clickstream-sink")],
        }
    )
    monkeypatch.setattr(ca, "http_request", script)
    result = ca.register_clickstream(branch="audit")
    assert result == {
        "connectors": {"clickstream-sink": "RUNNING"},
        "config": {
            "tasks.max": "1",
            "commit_interval_ms": "60000",
            "branch": "audit",
            "overrides": {},
        },
    }
    puts = [call for call in script.calls if call.method == "PUT"]
    assert len(puts) == 1
    assert json.loads(puts[0].body or b"{}") == ca.clickstream_sink_config(branch="audit")


def test_parse_overrides_reads_key_value_pairs_and_refuses_a_bare_word() -> None:
    assert ca.parse_overrides(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    with pytest.raises(ValueError, match="KEY=VALUE") as info:
        ca.parse_overrides(["leaky-value"])
    assert "leaky-value" not in str(info.value)


def test_the_lifecycle_verbs_accept_the_clickstream_sink() -> None:
    for verb in ("stop", "resume", "delete"):
        args = ca.build_parser().parse_args([verb, "clickstream-sink"])
        assert args.connector == "clickstream-sink"
    args = ca.build_parser().parse_args(
        ["register-clickstream", "--tasks-max", "3", "--override", "a=b", "--branch", "audit"]
    )
    assert (args.tasks_max, args.override, args.branch) == (3, ["a=b"], "audit")


# --- registration and polling -------------------------------------------------------------------


@dataclass
class Call:
    method: str
    url: str
    body: bytes | None


@dataclass
class Script:
    """Scripted HTTP responses keyed by (method, path), each consumed in order; every call recorded."""

    responses: dict[tuple[str, str], list[tuple[int, bytes]]]
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append(Call(method, url, body))
        path = url.removeprefix(ca.CONNECT_URL)
        queue = self.responses[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def running(name: str) -> tuple[int, bytes]:
    report = {
        "name": name,
        "connector": {"state": "RUNNING", "worker_id": "connect:8083"},
        "tasks": [{"id": 0, "state": "RUNNING", "worker_id": "connect:8083"}],
    }
    return 200, json.dumps(report).encode()


PLUGINS = [
    {
        "class": "io.debezium.connector.postgresql.PostgresConnector",
        "type": "source",
        "version": "3.6.3.Final",
    },
    {
        "class": "org.apache.iceberg.connect.IcebergSinkConnector",
        "type": "sink",
        "version": "1.11.0",
    },
]


def happy_script() -> Script:
    return Script(
        {
            ("PUT", f"/connectors/{ca.SOURCE_NAME}/config"): [(201, b"{}")],
            ("PUT", f"/connectors/{ca.SINK_NAME}/config"): [(201, b"{}")],
            ("GET", f"/connectors/{ca.SOURCE_NAME}/status"): [running(ca.SOURCE_NAME)],
            ("GET", f"/connectors/{ca.SINK_NAME}/status"): [running(ca.SINK_NAME)],
            ("GET", "/connector-plugins"): [(200, json.dumps(PLUGINS).encode())],
        }
    )


def test_register_puts_the_source_before_the_sink_then_polls_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = happy_script()
    monkeypatch.setattr(ca, "http_request", script)
    result = ca.register()
    sequence = [(call.method, call.url.removeprefix(ca.CONNECT_URL)) for call in script.calls]
    puts = [item for item in sequence if item[0] == "PUT"]
    assert puts == [
        ("PUT", "/connectors/shopstream-cdc/config"),
        ("PUT", "/connectors/bronze-sink/config"),
    ]
    assert sequence.index(puts[0]) < sequence.index(puts[1])
    first_status = next(i for i, item in enumerate(sequence) if item[1].endswith("/status"))
    assert first_status > sequence.index(puts[1])
    assert result["connectors"] == {"shopstream-cdc": "RUNNING", "bronze-sink": "RUNNING"}
    assert result["plugins"]["org.apache.iceberg.connect.IcebergSinkConnector"] == "1.11.0"
    body = json.loads(script.calls[0].body or b"{}")
    assert body == ca.debezium_config()


def test_put_config_raises_on_an_error_status_without_a_config_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaked = ca.debezium_config()["database.hostname"] + "-internal-host"
    error = {"error_code": 400, "message": f"bad value {ca.KARAPACE_URL} for key\nsecond line"}
    script = Script(
        {("PUT", "/connectors/shopstream-cdc/config"): [(400, json.dumps(error).encode())]}
    )
    monkeypatch.setattr(ca, "http_request", script)
    with pytest.raises(RuntimeError, match="HTTP 400") as info:
        ca.put_config(ca.SOURCE_NAME, ca.debezium_config())
    assert ca.KARAPACE_URL not in str(info.value)
    assert "second line" not in str(info.value)
    assert leaked not in str(info.value)


def test_state_of_needs_the_connector_and_every_task_running() -> None:
    def report(connector: str, *tasks: str) -> dict[str, Any]:
        return {
            "connector": {"state": connector},
            "tasks": [{"id": i, "state": state} for i, state in enumerate(tasks)],
        }

    assert ca.state_of(report("RUNNING", "RUNNING")) == "RUNNING"
    assert ca.state_of(report("RUNNING", "RUNNING", "FAILED")) == "FAILED"
    assert ca.state_of(report("RUNNING", "UNASSIGNED")) == "UNASSIGNED"
    assert ca.state_of(report("RUNNING")) == "NO_TASKS"
    assert ca.state_of(report("PAUSED", "RUNNING")) == "PAUSED"
    assert ca.state_of({}) == "UNKNOWN"


def test_wait_running_polls_until_the_tasks_are_running(monkeypatch: pytest.MonkeyPatch) -> None:
    starting = (
        200,
        json.dumps({"connector": {"state": "RUNNING"}, "tasks": []}).encode(),
    )
    script = Script(
        {("GET", "/connectors/shopstream-cdc/status"): [starting, starting, running("x")]}
    )
    monkeypatch.setattr(ca, "http_request", script)
    slept: list[float] = []
    assert ca.wait_running(ca.SOURCE_NAME, sleep=slept.append) == "RUNNING"
    assert slept == [2.0, 2.0]


def test_wait_running_waits_through_a_status_that_does_not_exist_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Script(
        {
            ("GET", "/connectors/shopstream-cdc/status"): [
                (404, b'{"message": "No status found for connector shopstream-cdc"}'),
                running("x"),
            ]
        }
    )
    monkeypatch.setattr(ca, "http_request", script)
    slept: list[float] = []
    assert ca.wait_running(ca.SOURCE_NAME, sleep=slept.append) == "RUNNING"
    assert slept == [2.0]


def test_wait_running_raises_on_a_failed_task_and_the_message_holds_no_config_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_uri = ca.sink_config()["iceberg.catalog.uri"]
    trace = (
        f"org.apache.kafka.connect.errors.ConnectException: cannot reach {catalog_uri}\n\tat x.y"
    )
    failed = {
        "connector": {"state": "RUNNING"},
        "tasks": [{"id": 0, "state": "FAILED", "trace": trace}],
    }
    script = Script(
        {("GET", "/connectors/bronze-sink/status"): [(200, json.dumps(failed).encode())]}
    )
    monkeypatch.setattr(ca, "http_request", script)
    with pytest.raises(RuntimeError, match="FAILED") as info:
        ca.wait_running(ca.SINK_NAME, sleep=lambda _: None)
    message = str(info.value)
    assert "ConnectException" in message
    assert catalog_uri not in message
    assert "\tat" not in message
    assert len(script.calls) == 1


def test_wait_running_times_out_with_the_last_state(monkeypatch: pytest.MonkeyPatch) -> None:
    paused = (200, json.dumps({"connector": {"state": "PAUSED"}, "tasks": []}).encode())
    monkeypatch.setattr(
        ca, "http_request", Script({("GET", "/connectors/bronze-sink/status"): [paused]})
    )
    now = iter([0.0, 1.0, 100.0, 200.0])
    with pytest.raises(TimeoutError, match="PAUSED"):
        ca.wait_running(ca.SINK_NAME, timeout_s=50, sleep=lambda _: None, clock=lambda: next(now))


def test_a_missing_connector_raises_with_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script(
        {("GET", "/connectors/bronze-sink/status"): [(404, b'{"message": "not found"}')]}
    )
    monkeypatch.setattr(ca, "http_request", script)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        ca.connector_status(ca.SINK_NAME)


def test_plugins_maps_class_to_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ca, "http_request", happy_script())
    found = ca.plugins()
    assert found["io.debezium.connector.postgresql.PostgresConnector"] == "3.6.3.Final"


# --- the CLI ------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [[], ["nonsense"], ["register", "status"]])
def test_usage_errors_exit_2(argv: list[str]) -> None:
    assert ca.main(argv) == 2


def test_register_prints_one_json_line_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ca, "http_request", happy_script())
    assert ca.main(["register"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["connectors"] == {
        "bronze-sink": "RUNNING",
        "shopstream-cdc": "RUNNING",
    }


def test_status_exits_1_when_a_connector_is_not_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = (200, json.dumps({"connector": {"state": "FAILED"}, "tasks": []}).encode())
    script = Script(
        {
            ("GET", f"/connectors/{ca.SOURCE_NAME}/status"): [running(ca.SOURCE_NAME)],
            ("GET", f"/connectors/{ca.SINK_NAME}/status"): [failed],
        }
    )
    monkeypatch.setattr(ca, "http_request", script)
    assert ca.main(["status"]) == 1
    assert json.loads(capsys.readouterr().out)["connectors"]["bronze-sink"] == "FAILED"


def test_a_network_error_fails_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def down(*args: object, **kwargs: object) -> tuple[int, bytes]:
        raise ConnectionRefusedError

    monkeypatch.setattr(ca, "http_request", down)
    assert ca.main(["register"]) == 1
    assert "ConnectionRefusedError" in capsys.readouterr().err


# --- PLAT-08: placeholders and plaintext occurrences ---------------------------------------------


def test_a_placeholder_is_found_and_listed_once() -> None:
    value = secrets.token_hex(16)
    text = (
        '{"a": "${env:CDC_DB_PASSWORD}", "b": "${env:CDC_DB_PASSWORD}", "c": "${env:OTHER_NAME}"}'
    )
    report = ca.placeholder_report(text, value)
    assert report["placeholders"] == ["${env:CDC_DB_PASSWORD}", "${env:OTHER_NAME}"]
    assert report["plaintext_secret_occurrences"] == 0


def test_the_raw_and_the_json_escaped_secret_are_both_counted() -> None:
    value = 'p"a\\ss-' + secrets.token_hex(8)
    escaped = json.dumps(value)[1:-1]
    assert escaped != value
    text = f"raw {value} and json {escaped} and again {escaped}"
    assert ca.placeholder_report(text, value)["plaintext_secret_occurrences"] == 3


def test_a_secret_with_no_escape_form_is_not_counted_twice() -> None:
    value = secrets.token_hex(16)
    assert ca.placeholder_report(f"x {value} y", value)["plaintext_secret_occurrences"] == 1


def test_a_short_secret_is_refused_without_naming_it() -> None:
    short = secrets.token_hex(8)[: ca.MIN_SECRET_CHARS - 1]
    assert len(short) == ca.MIN_SECRET_CHARS - 1
    with pytest.raises(ValueError, match="too short") as info:
        ca.placeholder_report(f"text {short}", short)
    assert short not in str(info.value)
    ca.placeholder_report("text", short.ljust(ca.MIN_SECRET_CHARS, "x"))


def test_read_secret_prefers_the_environment_and_falls_back_to_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from_file = secrets.token_hex(8)
    from_env = secrets.token_hex(8)
    env_file = tmp_path / ".env"
    env_file.write_text(f"CDC_DB_PASSWORD={from_file}\n", encoding="utf-8")
    monkeypatch.setattr(ca, "DEFAULT_ENV_FILE", env_file)
    monkeypatch.delenv("CDC_DB_PASSWORD", raising=False)
    assert ca.read_secret("CDC_DB_PASSWORD") == from_file
    monkeypatch.setenv("CDC_DB_PASSWORD", from_env)
    assert ca.read_secret("CDC_DB_PASSWORD") == from_env


def test_read_secret_names_the_key_and_never_a_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = secrets.token_hex(8)
    env_file = tmp_path / ".env"
    env_file.write_text(f"SOMETHING_ELSE={other}\nCDC_DB_PASSWORD=\n", encoding="utf-8")
    monkeypatch.setattr(ca, "DEFAULT_ENV_FILE", env_file)
    monkeypatch.delenv("CDC_DB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="CDC_DB_PASSWORD") as info:
        ca.read_secret("CDC_DB_PASSWORD")
    assert other not in str(info.value)
    monkeypatch.setattr(ca, "DEFAULT_ENV_FILE", tmp_path / "missing.env")
    with pytest.raises(ValueError, match="CDC_DB_PASSWORD"):
        ca.read_secret("CDC_DB_PASSWORD")


def feed(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))


def test_the_placeholders_command_prints_one_json_line_without_the_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value = secrets.token_hex(16)
    monkeypatch.setenv("CDC_DB_PASSWORD", value)
    feed(monkeypatch, b'{"database.password": "${env:CDC_DB_PASSWORD}"}\n')
    assert ca.main(["placeholders", "--key", "CDC_DB_PASSWORD", "--source", "rest-config"]) == 0
    out = capsys.readouterr().out
    assert value not in out
    assert json.loads(out) == {
        "source": "rest-config",
        "placeholders": ["${env:CDC_DB_PASSWORD}"],
        "plaintext_secret_occurrences": 0,
    }


def test_the_placeholders_command_counts_a_leak_and_decodes_bad_bytes_with_replacement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value = secrets.token_hex(16)
    monkeypatch.setenv("CDC_DB_PASSWORD", value)
    feed(monkeypatch, b"\xff\xfe log line " + value.encode() + b" \x80 tail\n")
    assert ca.main(["placeholders", "--key", "CDC_DB_PASSWORD", "--source", "worker-log"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plaintext_secret_occurrences"] == 1
    assert report["source"] == "worker-log"


def test_the_placeholders_command_refuses_a_short_secret_and_prints_no_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    short = "abc1234"
    monkeypatch.setenv("CDC_DB_PASSWORD", short)
    feed(monkeypatch, b"text")
    assert ca.main(["placeholders", "--key", "CDC_DB_PASSWORD", "--source", "x"]) == 1
    captured = capsys.readouterr()
    assert short not in captured.out + captured.err
    assert "too short" in captured.err


def test_show_config_prints_the_connector_json_for_piping(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ca.main(["show-config", "source"]) == 0
    assert json.loads(capsys.readouterr().out) == ca.debezium_config()
    assert ca.main(["show-config", "sink"]) == 0
    assert json.loads(capsys.readouterr().out) == ca.sink_config()
    assert ca.main(["show-config", "other"]) == 2


# --- stop, resume, delete and the offsets PATCH (Plan 04-03) -------------------------------------


def status_body(connector: str, *tasks: str) -> tuple[int, bytes]:
    report = {
        "connector": {"state": connector},
        "tasks": [{"id": i, "state": state} for i, state in enumerate(tasks)],
    }
    return 200, json.dumps(report).encode()


def test_stop_resume_and_delete_send_the_right_method_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Script(
        {
            ("PUT", "/connectors/bronze-sink/stop"): [(204, b"")],
            ("PUT", "/connectors/bronze-sink/resume"): [(202, b"")],
            ("DELETE", "/connectors/bronze-sink"): [(204, b"")],
        }
    )
    monkeypatch.setattr(ca, "http_request", script)
    ca.stop(ca.SINK_NAME)
    ca.resume(ca.SINK_NAME)
    ca.delete(ca.SINK_NAME)
    assert [(c.method, c.url.removeprefix(ca.CONNECT_URL)) for c in script.calls] == [
        ("PUT", "/connectors/bronze-sink/stop"),
        ("PUT", "/connectors/bronze-sink/resume"),
        ("DELETE", "/connectors/bronze-sink"),
    ]


def test_delete_accepts_a_missing_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    script = Script({("DELETE", "/connectors/bronze-sink"): [(404, b'{"message": "gone"}')]})
    monkeypatch.setattr(ca, "http_request", script)
    ca.delete(ca.SINK_NAME)


@pytest.mark.parametrize(
    ("call", "key", "status"),
    [
        (ca.stop, ("PUT", "/connectors/bronze-sink/stop"), 409),
        (ca.resume, ("PUT", "/connectors/bronze-sink/resume"), 500),
        (ca.delete, ("DELETE", "/connectors/bronze-sink"), 403),
    ],
)
def test_an_unexpected_status_raises_with_the_status_and_no_body(
    monkeypatch: pytest.MonkeyPatch, call: Any, key: tuple[str, str], status: int
) -> None:
    secret_body = b'{"message": "internal host connect-7 refused"}'
    monkeypatch.setattr(ca, "http_request", Script({key: [(status, secret_body)]}))
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as info:
        call(ca.SINK_NAME)
    assert "connect-7" not in str(info.value)
    assert "refused" not in str(info.value)


def test_wait_stopped_needs_the_state_stopped_and_no_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = Script(
        {
            ("GET", "/connectors/bronze-sink/status"): [
                status_body("RUNNING", "RUNNING"),
                status_body("STOPPED", "RUNNING"),
                status_body("STOPPED"),
            ]
        }
    )
    monkeypatch.setattr(ca, "http_request", script)
    slept: list[float] = []
    assert ca.wait_stopped(ca.SINK_NAME, sleep=slept.append) == "STOPPED"
    assert slept == [2.0, 2.0]


def test_wait_stopped_times_out_with_the_last_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ca,
        "http_request",
        Script({("GET", "/connectors/bronze-sink/status"): [status_body("RUNNING", "RUNNING")]}),
    )
    now = iter([0.0, 1.0, 100.0, 200.0])
    with pytest.raises(TimeoutError, match="RUNNING"):
        ca.wait_stopped(ca.SINK_NAME, timeout_s=50, sleep=lambda _: None, clock=lambda: next(now))


# --- the offsets PATCH body ---------------------------------------------------------------------


def test_offsets_body_is_exactly_the_documented_shape_sorted_with_integers() -> None:
    topic = "shopstream.public.customers"
    body = ca.offsets_body({(topic, 1): 42, (topic, 0): 9})
    assert body == {
        "offsets": [
            {
                "partition": {"kafka_topic": topic, "kafka_partition": 0},
                "offset": {"kafka_offset": 9},
            },
            {
                "partition": {"kafka_topic": topic, "kafka_partition": 1},
                "offset": {"kafka_offset": 42},
            },
        ]
    }
    text = json.dumps(body)
    assert not re.search(r'"\d+"', text)
    assert re.search(r'"kafka_partition": 0\b', text)


def test_offsets_body_sorts_by_topic_then_partition() -> None:
    body = ca.offsets_body({("b", 0): 1, ("a", 2): 1, ("a", 10): 1, ("a", 1): 1})
    keys = [
        (o["partition"]["kafka_topic"], o["partition"]["kafka_partition"]) for o in body["offsets"]
    ]
    assert keys == [("a", 1), ("a", 2), ("a", 10), ("b", 0)]


def test_offsets_body_refuses_a_negative_or_non_integer_offset_and_an_empty_map() -> None:
    with pytest.raises(ValueError, match="offset"):
        ca.offsets_body({("t", 0): -1})
    with pytest.raises(ValueError, match="offset"):
        ca.offsets_body({("t", 0): "5"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="partition"):
        ca.offsets_body({})


def test_patch_offsets_sends_the_body_and_accepts_only_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = ca.offsets_body({("t", 0): 3})
    script = Script({("PATCH", "/connectors/bronze-sink/offsets"): [(200, b'{"message": "ok"}')]})
    monkeypatch.setattr(ca, "http_request", script)
    assert ca.patch_offsets(ca.SINK_NAME, body) == 200
    (call,) = script.calls
    assert call.method == "PATCH"
    assert json.loads(call.body or b"{}") == body
    refused = Script({("PATCH", "/connectors/bronze-sink/offsets"): [(400, b'{"message": "x"}')]})
    monkeypatch.setattr(ca, "http_request", refused)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        ca.patch_offsets(ca.SINK_NAME, body)


# --- register without a commit branch -----------------------------------------------------------


def test_register_no_branch_puts_a_sink_config_without_the_commit_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = happy_script()
    monkeypatch.setattr(ca, "http_request", script)
    ca.register(branch=None)
    sink_put = next(c for c in script.calls if c.url.endswith("/connectors/bronze-sink/config"))
    sent = json.loads(sink_put.body or b"{}")
    assert "iceberg.tables.default-commit-branch" not in sent
    assert sent == ca.sink_config(None)


def test_register_defaults_to_the_audit_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    script = happy_script()
    monkeypatch.setattr(ca, "http_request", script)
    ca.register()
    sink_put = next(c for c in script.calls if c.url.endswith("/connectors/bronze-sink/config"))
    assert json.loads(sink_put.body or b"{}")["iceberg.tables.default-commit-branch"] == "audit"


def test_the_cli_register_no_branch_flag_and_the_lifecycle_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = happy_script()
    monkeypatch.setattr(ca, "http_request", script)
    assert ca.main(["register", "--no-branch"]) == 0
    sink_put = next(c for c in script.calls if c.url.endswith("/connectors/bronze-sink/config"))
    assert "iceberg.tables.default-commit-branch" not in json.loads(sink_put.body or b"{}")
    capsys.readouterr()
    lifecycle = Script(
        {
            ("PUT", "/connectors/bronze-sink/stop"): [(204, b"")],
            ("GET", "/connectors/bronze-sink/status"): [
                status_body("STOPPED"),
                status_body("RUNNING", "RUNNING"),
            ],
            ("PUT", "/connectors/bronze-sink/resume"): [(202, b"")],
            ("DELETE", "/connectors/bronze-sink"): [(204, b"")],
        }
    )
    monkeypatch.setattr(ca, "http_request", lifecycle)
    for command in ("stop", "resume", "delete"):
        assert ca.main([command]) == 0
        assert json.loads(capsys.readouterr().out)["connector"] == "bronze-sink"
    assert ca.main(["stop", "not-a-connector"]) == 2
