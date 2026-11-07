"""The canonical line: one operation, one byte string (D-19, D-20, ADR-005 "Determinism hash").

A line is JSON with sorted keys, `,` and `:` separators, UTF-8 without `\\u` escapes, and one
`\\n`. Values are encoded by exact type, so nothing passes by accident: `json.dumps` would
happily write a float or a `bool` where a number belongs, so the walk here runs first and
raises on anything it doesn't recognise.

`CanonError` names the type of the offending value and its path (`row.list_price`), never the
value: generated records are personal-looking data, and public CI logs are copies erasure
can't reach (ADR-005).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

from . import clock
from .ops import Op, OpKind

MONEY_EXPONENT = -2

_MICROSECOND = timedelta(microseconds=1)
_NO_OFFSET = timedelta(0)


class CanonError(TypeError):
    """A value the encoder refuses; the message names its type and path, never the value."""


def _refuse(value: object, path: str) -> CanonError:
    return CanonError(f"{path}: unsupported value of type {type(value).__name__}")


def encode(obj: object, path: str = "value") -> object:
    """Walk `obj` and return the JSON-safe form of it, or raise CanonError."""
    kind = type(obj)
    if obj is None or kind is bool or kind is int or kind is str:
        return obj
    if kind is Decimal:
        if not isinstance(obj, Decimal) or not obj.is_finite():
            raise _refuse(obj, path)
        if obj.as_tuple().exponent != MONEY_EXPONENT:
            raise CanonError(f"{path}: a Decimal must be at exponent {MONEY_EXPONENT}")
        return format(obj, "f")
    if kind is datetime:
        if not isinstance(obj, datetime) or obj.utcoffset() != _NO_OFFSET:
            raise CanonError(f"{path}: a datetime must be aware and in UTC")
        return clock.render((obj - clock.EPOCH) // _MICROSECOND)
    if kind is dict:
        if not isinstance(obj, dict):
            raise _refuse(obj, path)
        out: dict[str, object] = {}
        for key, value in obj.items():
            if type(key) is not str:
                raise CanonError(f"{path}: a key of type {type(key).__name__}")
            out[key] = encode(value, f"{path}.{key}")
        return out
    if kind is list or kind is tuple:
        if not isinstance(obj, list | tuple):
            raise _refuse(obj, path)
        return [encode(item, f"{path}[{index}]") for index, item in enumerate(obj)]
    raise _refuse(obj, path)


def dumps(obj: object) -> str:
    """Canonical JSON text for `obj` (after the type walk)."""
    return json.dumps(encode(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def line(seq: int, op: Op) -> bytes:
    """The canonical line for one operation of the tick numbered `seq`.

    A delete carries its key only; insert and update carry the full after-image.
    """
    body: dict[str, object] = {
        "seq": encode(seq, "seq"),
        "op": op.kind.value,
        "key": encode(dict(op.key), "key"),
    }
    if op.kind is not OpKind.DELETE:
        body["row"] = encode(dict(op.row or {}), "row")
    return dumps(body).encode("utf-8") + b"\n"
