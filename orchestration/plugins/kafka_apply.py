"""The apply function for the fx.refresh asset watcher (ADR-001 item 6).

This module must be importable by the triggerer, which is why it sits in the plugins folder and not
beside the DAG: the triggerer does not put /opt/airflow/dags on its path, but it does import
/opt/airflow/plugins. Restart the triggerer after any edit. Standard library only.

The function returns a JSON event for every message, so the event that starts the DAG run carries
the message's topic, partition, offset, Kafka timestamp and value. A null value (a tombstone) and
an empty value both give an event, and bytes that are not UTF-8 are decoded with replacement
characters, so no message can raise inside the triggerer and crash-loop the trigger.
"""

from __future__ import annotations

import json
from typing import Any


def apply_function(message: Any) -> str:
    """The JSON event for one Kafka message: topic, partition, offset, timestamp_ms and value."""
    raw = message.value()
    value = None if raw is None else bytes(raw).decode("utf-8", errors="replace")
    return json.dumps(
        {
            "topic": message.topic(),
            "partition": message.partition(),
            "offset": message.offset(),
            "timestamp_ms": message.timestamp()[1],
            "value": value,
        }
    )
