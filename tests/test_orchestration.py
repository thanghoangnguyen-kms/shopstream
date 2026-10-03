"""Tests for the item 6 DAG and its apply function. No Airflow and no Kafka.

The apply function is stdlib only, so it is loaded from its file and called on a fake message. The
DAG imports Airflow, which CI does not install, so it is checked as text and never imported.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "orchestration" / "plugins" / "kafka_apply.py"
DAG = REPO / "orchestration" / "dags" / "fx_refresh_on_message.py"
EVENT_KEYS = {"topic", "partition", "offset", "timestamp_ms", "value"}


def load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kafka_apply", PLUGIN)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMessage:
    """The confluent_kafka.Message methods the apply function calls."""

    def __init__(self, value: bytes | None) -> None:
        self._value = value

    def topic(self) -> str:
        return "fx.refresh"

    def partition(self) -> int:
        return 2

    def offset(self) -> int:
        return 41

    def timestamp(self) -> tuple[int, int]:
        return (1, 1_790_000_000_123)

    def value(self) -> bytes | None:
        return self._value


def event(value: bytes | None) -> dict[str, object]:
    parsed = json.loads(load_plugin().apply_function(FakeMessage(value)))
    assert isinstance(parsed, dict)
    return parsed


def test_a_normal_value_round_trips_with_exactly_the_five_keys() -> None:
    parsed = event(b"refresh-1")
    assert set(parsed) == EVENT_KEYS
    assert parsed == {
        "topic": "fx.refresh",
        "partition": 2,
        "offset": 41,
        "timestamp_ms": 1_790_000_000_123,
        "value": "refresh-1",
    }


def test_a_null_value_gives_a_null_value_and_no_exception() -> None:
    parsed = event(None)
    assert set(parsed) == EVENT_KEYS
    assert parsed["value"] is None


def test_an_empty_value_gives_an_empty_string() -> None:
    assert event(b"")["value"] == ""


def test_bytes_that_are_not_utf8_get_replacement_characters_and_no_exception() -> None:
    value = event(b"\xff\xfe")["value"]
    assert value == "��"


def test_the_event_is_a_json_string() -> None:
    assert isinstance(load_plugin().apply_function(FakeMessage(b"x")), str)


@pytest.mark.parametrize(
    "fragment",
    [
        'scheme="kafka"',
        'topics=["fx.refresh"]',
        'apply_function="kafka_apply.apply_function"',
        'kafka_config_id="kafka_default"',
        "AssetWatcher",
        "schedule=[asset]",
        'dag_id="fx_refresh_on_message"',
    ],
)
def test_the_dag_names_the_trigger_the_asset_and_the_schedule(fragment: str) -> None:
    assert fragment in DAG.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [DAG, PLUGIN])
def test_neither_file_holds_a_secret_or_a_broker_address(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    assert "password" not in text
    assert "bootstrap.servers" not in text
    assert "kafka-1" not in text
