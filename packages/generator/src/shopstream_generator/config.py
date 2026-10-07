"""The model config: seed, simulated range and volumes, all integers (D-08, CORE-05 in part).

This is the tracer's slice of the config. Every key is mandatory and an unknown key is an
error, so a typo can't silently fall back to a default. No float enters: `load` refuses one at
parse time, and `from_json` accepts only exact `int`s (a `bool` is not one). Times are written
and read in the one canonical form `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

`ConfigError` names the key path and never the value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import canon, clock

SEED_LIMIT = 2**63


class ConfigError(ValueError):
    """The config is not one this generator accepts; the message names a key path."""


@dataclass(frozen=True)
class Volumes:
    initial_customers: int
    customers_per_day: int


@dataclass(frozen=True)
class ModelConfig:
    seed: int
    start_us: int
    end_us: int
    volumes: Volumes

    def to_json(self) -> dict[str, object]:
        return {
            "range": {"start": clock.render(self.start_us), "end": clock.render(self.end_us)},
            "seed": self.seed,
            "volumes": {
                "initial_customers": self.volumes.initial_customers,
                "customers_per_day": self.volumes.customers_per_day,
            },
        }

    def config_sha256(self) -> str:
        return hashlib.sha256(canon.dumps(self.to_json()).encode("utf-8")).hexdigest()


def _mapping(obj: object, path: str, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(obj, dict):
        raise ConfigError(f"{path}: expected an object")
    found = set(obj)
    if missing := sorted(keys - found):
        raise ConfigError(f"{path}: missing key {missing[0]}")
    if unknown := sorted(found - keys):
        raise ConfigError(f"{path}: unknown key {unknown[0]}")
    return obj


def _int(value: object, path: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{path}: expected an integer")
    return value


def _time(value: object, path: str) -> int:
    if not isinstance(value, str):
        raise ConfigError(f"{path}: expected a time string")
    try:
        return clock.parse(value)
    except ValueError:
        raise ConfigError(f"{path}: not a YYYY-MM-DDTHH:MM:SS.ffffffZ time") from None


def from_json(obj: object) -> ModelConfig:
    """Build a ModelConfig from parsed JSON, or raise ConfigError."""
    top = _mapping(obj, "config", {"range", "seed", "volumes"})
    window = _mapping(top["range"], "range", {"start", "end"})
    volumes = _mapping(top["volumes"], "volumes", {"initial_customers", "customers_per_day"})
    seed = _int(top["seed"], "seed")
    if not 0 <= seed < SEED_LIMIT:
        raise ConfigError("seed: must be in [0, 2**63)")
    start_us = _time(window["start"], "range.start")
    end_us = _time(window["end"], "range.end")
    if start_us >= end_us:
        raise ConfigError("range: start must be before end")
    initial_customers = _int(volumes["initial_customers"], "volumes.initial_customers")
    customers_per_day = _int(volumes["customers_per_day"], "volumes.customers_per_day")
    if initial_customers < 0:
        raise ConfigError("volumes.initial_customers: must not be negative")
    if not 0 <= customers_per_day <= clock.US_PER_DAY:
        raise ConfigError("volumes.customers_per_day: must be in [0, 86_400_000_000]")
    return ModelConfig(seed, start_us, end_us, Volumes(initial_customers, customers_per_day))


def _refuse_float(_: str) -> object:
    raise ConfigError("config: a float is not allowed anywhere")


def load(path: Path) -> ModelConfig:
    """Read a UTF-8 JSON config file; a float anywhere in it is an error (D-08)."""
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text, parse_float=_refuse_float, parse_constant=_refuse_float)
    except json.JSONDecodeError:
        raise ConfigError("config: not valid JSON") from None
    return from_json(parsed)
