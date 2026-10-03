"""Register the CDC connectors with Kafka Connect and read their state: the `streaming` profile's client.

Run inside the `spark-job` one-shot, which shares the Compose network with Connect and Karapace:
`python /app/connect_admin.py register`. Connector configs are Python functions here, not JSON files,
so they are unit-tested and the spike image needs no extra mount. `debezium_config` carries the
database password only as the placeholder `${env:CDC_DB_PASSWORD}`, which the Connect worker resolves
through Kafka's EnvVarConfigProvider (allowlisted for that one name). No function in this module
prints a config value: a failure names the connector, the HTTP status or the first line of a task's
trace with every config value scrubbed out.

Registration is deliberately not part of `just up` (research A8; PLAT-04 is Phase 5). Pure and tested
without a network: the config builders, `check_url`, `state_of` and the polling loop (through an
injected `http_request`). The only network calls go through the origin-allowlisted `http_request`.

CLI: `register` prints one JSON line `{"connectors": {name: state}, "plugins": {class: version}}`;
`status` prints the connectors' states. Exit 0 on success, 1 on a failed connector or an error, 2 on
usage.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from read_v3 import error_text, scrub_values

CONNECT_URL = "http://connect:8083"
KARAPACE_URL = "http://karapace:8081"
ALLOWED_ORIGINS = frozenset({CONNECT_URL, KARAPACE_URL})
TIMEOUT_S = 30
SOURCE_NAME = "shopstream-cdc"
SINK_NAME = "bronze-sink"
TABLES = ("customers", "products", "orders", "order_items", "reviews")
TOPIC_PREFIX = "shopstream"
CONTROL_TOPIC = "control-iceberg"
SECRET_KEY = "CDC_DB_PASSWORD"
RUNNING = "RUNNING"
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
POLL_INTERVAL_S = 2.0
ERROR_LIMIT = 200


def check_url(url: str) -> None:
    """Refuse any URL that is not plain http to Connect or Karapace."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http":
        raise ValueError("only http URLs are allowed")
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in ALLOWED_ORIGINS:
        raise ValueError(f"origin {origin} is not allowed")


def http_request(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """Send one request and return (status, body); an HTTP error status is returned, not raised."""
    check_url(url)
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload: bytes = response.read()
            return int(response.status), payload
    except urllib.error.HTTPError as exc:
        error_payload: bytes = exc.read()
        return int(exc.code), error_payload


def topic(table: str) -> str:
    """The Kafka topic Debezium writes a public table to."""
    return f"{TOPIC_PREFIX}.public.{table}"


def _avro_converters() -> dict[str, str]:
    return {
        "key.converter": "io.confluent.connect.avro.AvroConverter",
        "key.converter.schema.registry.url": KARAPACE_URL,
        "value.converter": "io.confluent.connect.avro.AvroConverter",
        "value.converter.schema.registry.url": KARAPACE_URL,
    }


def debezium_config() -> dict[str, str]:
    """The Debezium Postgres source. The password is a placeholder, never the secret."""
    return {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "tasks.max": "1",
        "database.hostname": "postgres",
        "database.port": "5432",
        "database.dbname": "shopstream",
        "database.user": "cdc",
        "database.password": f"${{env:{SECRET_KEY}}}",
        "topic.prefix": TOPIC_PREFIX,
        "plugin.name": "pgoutput",
        "slot.name": "shopstream_dbz",
        "publication.name": "shopstream_cdc",
        "publication.autocreate.mode": "disabled",
        "table.include.list": ",".join(f"public.{table}" for table in TABLES),
        "snapshot.mode": "initial",
        "tombstones.on.delete": "true",
        "heartbeat.interval.ms": "10000",
        **_avro_converters(),
    }


def sink_config(branch: str | None = "audit") -> dict[str, str]:
    """The Iceberg sink: raw Debezium envelopes into bronze.<table> on `branch`.

    tasks.max is 1 because a task that never received a record never answers START_COMMIT, so a
    wider connector stalls every commit round for 30 s. Only KafkaMetadataTransform runs: Iceberg's
    Debezium transform would drop source.lsn and map a snapshot read to an insert. Without
    route-field the sink would write every record to every table.
    """
    config: dict[str, str] = {
        "connector.class": "org.apache.iceberg.connect.IcebergSinkConnector",
        "tasks.max": "1",
        "topics": ",".join(topic(table) for table in TABLES),
        "iceberg.tables": ",".join(f"bronze.{table}" for table in TABLES),
        "iceberg.tables.route-field": "_kafka_metadata_topic",
    }
    for table in TABLES:
        config[f"iceberg.table.bronze.{table}.route-regex"] = rf"{TOPIC_PREFIX}\.public\.{table}"
    config.update(
        {
            "iceberg.tables.auto-create-enabled": "true",
            "iceberg.tables.evolve-schema-enabled": "true",
            "iceberg.tables.auto-create-props.format-version": "2",
        }
    )
    if branch is not None:
        config["iceberg.tables.default-commit-branch"] = branch
    config.update(
        {
            "iceberg.control.topic": CONTROL_TOPIC,
            "iceberg.control.commit.interval-ms": "15000",
            "iceberg.catalog": "lk",
            "iceberg.catalog.type": "rest",
            "iceberg.catalog.uri": "http://lakekeeper:8181/catalog",
            "iceberg.catalog.warehouse": "spike",
            "iceberg.catalog.header.X-Iceberg-Access-Delegation": "vended-credentials",
            **_avro_converters(),
            "transforms": "kafkaMeta",
            "transforms.kafkaMeta.type": "org.apache.iceberg.connect.transforms.KafkaMetadataTransform",
        }
    )
    return config


def _config_for(name: str) -> dict[str, str]:
    return debezium_config() if name == SOURCE_NAME else sink_config()


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _error_line(name: str, status: int, body: bytes) -> str:
    """`name`, the HTTP status and the first line of Connect's message, scrubbed of config values."""
    parsed = _json(body)
    message = parsed.get("message", "") if isinstance(parsed, dict) else ""
    lines = str(message).splitlines()
    first = scrub_values(lines[0] if lines else "", list(_config_for(name).values()))
    return f"{name}: HTTP {status}: {first}"[:ERROR_LIMIT]


def put_config(name: str, config: Mapping[str, str]) -> None:
    """Create or update a connector (PUT is idempotent). Raises on any status but 200 and 201."""
    body = json.dumps(dict(config)).encode()
    status, payload = http_request(
        "PUT", f"{CONNECT_URL}/connectors/{name}/config", JSON_HEADERS, body
    )
    if status not in {200, 201}:
        raise RuntimeError(_error_line(name, status, payload))


def connector_status(name: str) -> dict[str, Any]:
    """GET /connectors/<name>/status as a dict; a missing connector raises."""
    status, payload = http_request("GET", f"{CONNECT_URL}/connectors/{name}/status", {}, None)
    parsed = _json(payload)
    if status != 200 or not isinstance(parsed, dict):
        raise RuntimeError(_error_line(name, status, payload))
    return parsed


def state_of(report: Mapping[str, Any]) -> str:
    """RUNNING only when the connector and at least one task are RUNNING and none is not.

    Otherwise the first state that is not RUNNING (FAILED, PAUSED, UNASSIGNED, ...), or NO_TASKS
    for a running connector that has not started a task yet.
    """
    connector = report.get("connector")
    connector_state = (
        str(connector.get("state", "UNKNOWN")) if isinstance(connector, dict) else "UNKNOWN"
    )
    if connector_state != RUNNING:
        return connector_state
    tasks = report.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "NO_TASKS"
    for task in tasks:
        task_state = str(task.get("state", "UNKNOWN")) if isinstance(task, dict) else "UNKNOWN"
        if task_state != RUNNING:
            return task_state
    return RUNNING


def _failed_task_line(name: str, report: Mapping[str, Any]) -> str:
    """The first line of the first FAILED task's trace, scrubbed of every config value."""
    values = list(_config_for(name).values())
    tasks = report.get("tasks")
    for task in tasks if isinstance(tasks, list) else []:
        if isinstance(task, dict) and task.get("state") == "FAILED":
            lines = str(task.get("trace", "")).splitlines()
            first = scrub_values(lines[0] if lines else "", values)
            return f"{name} task {task.get('id', '?')} FAILED: {first}"[:ERROR_LIMIT]
    return f"{name} FAILED"


def wait_running(
    name: str,
    timeout_s: float = 180,
    interval_s: float = POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll every `interval_s` until the connector and every task are RUNNING, and return RUNNING.

    A FAILED connector or task raises at once with the first trace line, scrubbed. A timeout raises
    with the last state seen.
    """
    deadline = clock() + timeout_s
    state = "UNKNOWN"
    while True:
        report = connector_status(name)
        state = state_of(report)
        if state == RUNNING:
            return state
        if state == "FAILED":
            raise RuntimeError(_failed_task_line(name, report))
        if clock() >= deadline:
            raise TimeoutError(f"{name} was not RUNNING after {timeout_s:g} s (last state {state})")
        sleep(interval_s)


def plugins() -> dict[str, str]:
    """GET /connector-plugins as {class: version}."""
    status, payload = http_request("GET", f"{CONNECT_URL}/connector-plugins", {}, None)
    parsed = _json(payload)
    if status != 200 or not isinstance(parsed, list):
        raise RuntimeError(f"connector-plugins: HTTP {status}")
    return {
        str(item["class"]): str(item.get("version", ""))
        for item in parsed
        if isinstance(item, dict) and "class" in item
    }


def register() -> dict[str, Any]:
    """Put the source, then the sink, then wait for both: {"connectors": {...}, "plugins": {...}}."""
    put_config(SOURCE_NAME, debezium_config())
    put_config(SINK_NAME, sink_config())
    states = {name: wait_running(name) for name in (SOURCE_NAME, SINK_NAME)}
    return {"connectors": states, "plugins": plugins()}


def _report() -> dict[str, str]:
    return {name: state_of(connector_status(name)) for name in (SOURCE_NAME, SINK_NAME)}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["register"], ["status"]):
        print("usage: connect_admin.py register | status", file=sys.stderr)
        return 2
    try:
        if args == ["register"]:
            print(json.dumps(register(), sort_keys=True), flush=True)
            return 0
        states = _report()
        print(json.dumps({"connectors": states}, sort_keys=True), flush=True)
        return 0 if all(state == RUNNING for state in states.values()) else 1
    except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
        print(f"error: {error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
