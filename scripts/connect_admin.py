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

PLAT-08's proof is `placeholders`: a capture from one of six sources is piped into it on the host and
it counts the env-provider placeholders and the real secret's plaintext occurrences (raw and
JSON-escaped), printing counts and never a value.

Plan 04-03 adds the lifecycle calls the rollback paths need: `stop`, `wait_stopped`, `resume`,
`delete`, and the offsets PATCH (`offsets_body`, `patch_offsets`). An unexpected status raises with
the connector name and the status only, never the response body.

CLI: `register [--no-branch]` prints one JSON line `{"connectors": {name: state}, "plugins": {class:
version}}` (`--no-branch` registers the sink without a commit branch, bronze append-only on main);
`status` prints the connectors' states; `stop`, `resume` and `delete` act on one connector (default
bronze-sink); `placeholders --key NAME --source LABEL` reads a capture on stdin; `show-config
source|sink` prints a connector config for piping. Exit 0 on success, 1 on a failed connector or an
error, 2 on usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import dotenv_lite
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
# Kafka's EnvVarConfigProvider reference, as it appears in a connector config.
PLACEHOLDER = re.compile(r"\$\{env:[A-Za-z_][A-Za-z0-9_]*\}")
# A shorter secret is refused: counting a common string would prove nothing.
MIN_SECRET_CHARS = 8
DEFAULT_ENV_FILE = dotenv_lite.DEFAULT_ENV_FILE


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


def connector_status(name: str, *, allow_pending: bool = False) -> dict[str, Any]:
    """GET /connectors/<name>/status as a dict; a missing connector raises.

    With `allow_pending`, a 404 returns {} instead: right after a PUT on a freshly started worker
    the connector exists before Connect has written its first status.
    """
    status, payload = http_request("GET", f"{CONNECT_URL}/connectors/{name}/status", {}, None)
    if allow_pending and status == 404:
        return {}
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
        report = connector_status(name, allow_pending=True)
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


def _expect(name: str, action: str, status: int, accepted: frozenset[int]) -> None:
    """Raise unless `status` is accepted; the message holds the name, action and status only."""
    if status not in accepted:
        raise RuntimeError(f"{name}: {action} returned HTTP {status}")


def stop(name: str) -> None:
    """PUT /connectors/<name>/stop (Connect answers 204); the connector ends STOPPED with no tasks."""
    status, _ = http_request("PUT", f"{CONNECT_URL}/connectors/{name}/stop", {}, None)
    _expect(name, "stop", status, frozenset({204}))


def resume(name: str) -> None:
    """PUT /connectors/<name>/resume (Connect answers 202)."""
    status, _ = http_request("PUT", f"{CONNECT_URL}/connectors/{name}/resume", {}, None)
    _expect(name, "resume", status, frozenset({202}))


def delete(name: str) -> None:
    """DELETE /connectors/<name>; 204 (deleted) and 404 (already gone) are both fine."""
    status, _ = http_request("DELETE", f"{CONNECT_URL}/connectors/{name}", {}, None)
    _expect(name, "delete", status, frozenset({204, 404}))


def wait_stopped(
    name: str,
    timeout_s: float = 120,
    interval_s: float = POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Poll until the connector's state is STOPPED and its task list is empty, and return STOPPED.

    A FAILED connector raises at once; a timeout raises with the last state seen. KIP-875's offsets
    PATCH needs exactly this state.
    """
    deadline = clock() + timeout_s
    while True:
        report = connector_status(name)
        connector = report.get("connector")
        state = str(connector.get("state", "UNKNOWN")) if isinstance(connector, dict) else "UNKNOWN"
        tasks = report.get("tasks")
        if state == "STOPPED" and not tasks:
            return state
        if state == "FAILED":
            raise RuntimeError(f"{name} is FAILED while waiting for STOPPED")
        if clock() >= deadline:
            raise TimeoutError(f"{name} was not STOPPED after {timeout_s:g} s (last state {state})")
        sleep(interval_s)


def offsets_body(resume_at: Mapping[tuple[str, int], int]) -> dict[str, Any]:
    """The PATCH /connectors/<name>/offsets body for {(topic, partition): next offset to read}.

    Exactly {"offsets": [{"partition": {"kafka_topic": T, "kafka_partition": P}, "offset":
    {"kafka_offset": N}}, ...]}, with partition and offset as JSON integers, sorted by (topic,
    partition). An empty map, a negative offset or a non-integer raises ValueError.
    """
    if not resume_at:
        raise ValueError("offsets_body needs at least one partition")
    entries = []
    for (topic_name, partition), offset in sorted(resume_at.items()):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("an offset must be a non-negative integer")
        if isinstance(partition, bool) or not isinstance(partition, int) or partition < 0:
            raise ValueError("a partition must be a non-negative integer")
        entries.append(
            {
                "partition": {"kafka_topic": topic_name, "kafka_partition": partition},
                "offset": {"kafka_offset": offset},
            }
        )
    return {"offsets": entries}


def patch_offsets(name: str, body: Mapping[str, Any]) -> int:
    """PATCH /connectors/<name>/offsets with `body`; only 200 is accepted and returned.

    The connector must be STOPPED (KIP-875). The error names the connector and the status only.
    """
    status, _ = http_request(
        "PATCH",
        f"{CONNECT_URL}/connectors/{name}/offsets",
        JSON_HEADERS,
        json.dumps(dict(body)).encode(),
    )
    _expect(name, "offsets patch", status, frozenset({200}))
    return status


def register(branch: str | None = "audit") -> dict[str, Any]:
    """Put the source, then the sink, then wait for both: {"connectors": {...}, "plugins": {...}}.

    `branch` is the sink's default commit branch; None registers it without one (bronze append-only
    on main, ADR-001's "branch doesn't work" configuration).
    """
    put_config(SOURCE_NAME, debezium_config())
    put_config(SINK_NAME, sink_config(branch))
    states = {name: wait_running(name) for name in (SOURCE_NAME, SINK_NAME)}
    return {"connectors": states, "plugins": plugins()}


def _report() -> dict[str, str]:
    return {name: state_of(connector_status(name)) for name in (SOURCE_NAME, SINK_NAME)}


def placeholder_report(text: str, secret: str) -> dict[str, Any]:
    """Count the env placeholders in `text` and the plaintext occurrences of `secret`.

    `plaintext_secret_occurrences` is the raw count plus the count of the secret's JSON-escaped form
    when that differs, so a copy escaped inside a JSON string cannot hide. A secret under
    MIN_SECRET_CHARS raises ValueError (its message never holds the secret).
    """
    if len(secret) < MIN_SECRET_CHARS:
        raise ValueError("secret too short to count")
    escaped = json.dumps(secret)[1:-1]
    occurrences = text.count(secret)
    if escaped != secret:
        occurrences += text.count(escaped)
    return {
        "placeholders": sorted(set(PLACEHOLDER.findall(text))),
        "plaintext_secret_occurrences": occurrences,
    }


def read_secret(key: str) -> str:
    """The value of `key` from the environment, else from infra/.env. Errors name the key only."""
    value = os.environ.get(key)
    if value:
        return value
    values = dotenv_lite.read_dotenv(DEFAULT_ENV_FILE)
    if values is not None and values.get(key):
        return values[key]
    raise ValueError(f"{key} is not set in the environment or in infra/.env")


def show_config(kind: str) -> None:
    """Print the Debezium or the sink config as JSON, for piping into `placeholders`."""
    configs = {"source": debezium_config, "sink": sink_config}
    if kind not in configs:
        raise ValueError("kind must be source or sink")
    print(json.dumps(configs[kind](), sort_keys=True, indent=2), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Register and inspect the CDC connectors.")
    commands = parser.add_subparsers(dest="command", required=True)
    registered = commands.add_parser("register", help="put both connectors and wait until they run")
    registered.add_argument(
        "--no-branch", action="store_true", help="register the sink without a commit branch"
    )
    commands.add_parser("status", help="print both connectors' states")
    for verb, text in (
        ("stop", "stop a connector and wait until it is STOPPED with no tasks"),
        ("resume", "resume a stopped connector and wait until it is RUNNING"),
        ("delete", "delete a connector (a missing one is fine)"),
    ):
        lifecycle = commands.add_parser(verb, help=text)
        lifecycle.add_argument(
            "connector", nargs="?", default=SINK_NAME, choices=(SOURCE_NAME, SINK_NAME)
        )
    counted = commands.add_parser("placeholders", help="count placeholders and plaintext on stdin")
    counted.add_argument("--key", required=True, help="name of the secret's environment variable")
    counted.add_argument("--source", required=True, help="label of the capture")
    shown = commands.add_parser("show-config", help="print a connector config as JSON")
    shown.add_argument("kind", choices=("source", "sink"))
    return parser


def lifecycle_command(verb: str, name: str) -> dict[str, str]:
    """Run stop, resume or delete on one connector; report the state it settled in."""
    if verb == "stop":
        stop(name)
        return {"connector": name, "state": wait_stopped(name)}
    if verb == "resume":
        resume(name)
        return {"connector": name, "state": wait_running(name)}
    delete(name)
    return {"connector": name, "state": "DELETED"}


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0
    try:
        if args.command == "register":
            print(
                json.dumps(register(None if args.no_branch else "audit"), sort_keys=True),
                flush=True,
            )
        elif args.command in {"stop", "resume", "delete"}:
            print(
                json.dumps(lifecycle_command(args.command, args.connector), sort_keys=True),
                flush=True,
            )
        elif args.command == "status":
            states = _report()
            print(json.dumps({"connectors": states}, sort_keys=True), flush=True)
            return 0 if all(state == RUNNING for state in states.values()) else 1
        elif args.command == "placeholders":
            text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            report = placeholder_report(text, read_secret(args.key))
            print(json.dumps({"source": args.source, **report}, sort_keys=True), flush=True)
        else:
            show_config(args.kind)
    except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
        print(f"error: {error_text(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
