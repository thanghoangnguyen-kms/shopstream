"""Unit tests for scripts/connect_admin.py. No network: http_request is replaced."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message
from io import BytesIO
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
