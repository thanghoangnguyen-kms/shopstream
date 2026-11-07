"""Test-only reader for the clickstream contract: pyyaml only, and it fails closed.

`datacontract lint` ignores a misspelled key inside a property, so this reader pins the
standard version and the allowed keys itself. The generator never imports it: its runtime
keeps its own page_type list and never reads contracts/ (D-15); Phase 3's P9 proves the two
agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

API_VERSION = "v3.2.0"
OBJECT_NAME = "page_view"
FIELDS = frozenset(
    {"event_id", "event_ts", "visitor_id", "customer_id", "page_type", "product_id", "referrer"}
)
PROPERTY_KEYS = frozenset(
    {
        "name",
        "logicalType",
        "logicalTypeOptions",
        "physicalType",
        "required",
        "primaryKey",
        "primaryKeyPosition",
        "classification",
        "tags",
        "enum",
        "description",
        "quality",
        "examples",
    }
)


class ContractError(ValueError):
    """The contract is not the one this reader understands."""


@dataclass(frozen=True)
class Contract:
    required: frozenset[str]
    page_types: tuple[str, ...]


def _mapping(node: object, what: str) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise ContractError(f"{what} is not a mapping")
    return node


def _enum_values(node: object) -> tuple[str, ...]:
    if not isinstance(node, list):
        return ()
    values: list[str] = []
    for entry in node:
        item = _mapping(entry, "an enum entry")
        value = item.get("value")
        if set(item) != {"value"} or not isinstance(value, str):
            raise ContractError("an enum entry is not {value: <string>}")
        values.append(value)
    if len(values) != len(set(values)):
        raise ContractError("the enum repeats a value")
    return tuple(values)


def parse(text: str) -> Contract:
    doc = _mapping(yaml.safe_load(text), "the contract")
    if doc.get("apiVersion") != API_VERSION:
        raise ContractError(f"apiVersion is not {API_VERSION}")
    objects = doc.get("schema")
    if not isinstance(objects, list) or len(objects) != 1:
        raise ContractError("schema must hold exactly one object")
    schema = _mapping(objects[0], "the schema object")
    if schema.get("name") != OBJECT_NAME:
        raise ContractError(f"the schema object is not {OBJECT_NAME}")
    properties = schema.get("properties")
    if not isinstance(properties, list):
        raise ContractError("the object has no properties list")
    required: set[str] = set()
    page_types: tuple[str, ...] = ()
    seen: list[str] = []
    for raw in properties:
        prop = _mapping(raw, "a property")
        unknown = set(prop) - PROPERTY_KEYS
        if unknown:
            raise ContractError(f"unknown property keys: {sorted(unknown)}")
        name = prop.get("name")
        if not isinstance(name, str):
            raise ContractError("a property has no name")
        seen.append(name)
        flag = prop.get("required", False)
        if not isinstance(flag, bool):
            raise ContractError(f"{name}: required is not a boolean")
        if flag:
            required.add(name)
        if name == "page_type":
            page_types = _enum_values(prop.get("enum"))
    if len(seen) != len(set(seen)) or set(seen) != FIELDS:
        raise ContractError("the properties are not the expected fields")
    if not page_types:
        raise ContractError("page_type has no enum")
    return Contract(required=frozenset(required), page_types=page_types)


def load(path: Path) -> Contract:
    return parse(path.read_text(encoding="utf-8"))
