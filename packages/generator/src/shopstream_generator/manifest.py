"""Per-stream digests, the manifest file and the run hash (D-21, ADR-005 "Determinism hash").

Each stream's digest is a running SHA-256 over its canonical lines in emission order, unsorted:
commit order is part of what is proven. The manifest always lists all seven streams, empty ones
included at count 0 and the SHA-256 of nothing, so a later phase changes a digest and never the
set of streams.

The manifest file is canonical JSON plus exactly one trailing newline (what the repo's
end-of-file hook would leave anyway), and the run hash is the SHA-256 of those exact bytes.
Nothing here prints a record: a mismatch report carries names, counts and digests only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from . import canon
from .ops import STREAMS, Tick

EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
FORMAT: Final = 1
_HEX = frozenset("0123456789abcdef")


def _refuse_float(_: str) -> object:
    raise ValueError("a manifest holds no float")


@dataclass(frozen=True)
class StreamDigest:
    count: int
    sha256: str


@dataclass(frozen=True)
class Manifest:
    """The seven streams' counts and digests plus the hash of the config that produced them."""

    config_sha256: str
    streams: tuple[tuple[str, StreamDigest], ...]

    def digest(self, name: str) -> StreamDigest:
        for stream_name, digest in self.streams:
            if stream_name == name:
                return digest
        raise KeyError(name)

    def to_bytes(self) -> bytes:
        body = {
            "config_sha256": self.config_sha256,
            "format": FORMAT,
            "streams": {
                name: {"count": digest.count, "sha256": digest.sha256}
                for name, digest in self.streams
            },
        }
        return canon.dumps(body).encode("utf-8") + b"\n"

    @classmethod
    def from_bytes(cls, data: bytes) -> Manifest:
        """Parse manifest bytes; anything but the exact canonical form raises ValueError."""
        parsed = json.loads(
            data.decode("utf-8"), parse_float=_refuse_float, parse_constant=_refuse_float
        )
        if not isinstance(parsed, dict) or set(parsed) != {"config_sha256", "format", "streams"}:
            raise ValueError("a manifest holds config_sha256, format and streams")
        if type(parsed["format"]) is not int or parsed["format"] != FORMAT:
            raise ValueError(f"manifest format must be {FORMAT}")
        config_sha256 = parsed["config_sha256"]
        streams = parsed["streams"]
        if not isinstance(config_sha256, str) or not isinstance(streams, dict):
            raise ValueError("manifest fields have the wrong type")
        if set(streams) != set(STREAMS):
            raise ValueError("a manifest lists exactly the seven streams")
        digests: list[tuple[str, StreamDigest]] = []
        for name in STREAMS:
            entry = streams[name]
            if not isinstance(entry, dict) or set(entry) != {"count", "sha256"}:
                raise ValueError(f"stream {name}: expected a count and a sha256")
            count, sha256 = entry["count"], entry["sha256"]
            if type(count) is not int or count < 0:
                raise ValueError(f"stream {name}: count must be a non-negative integer")
            if not isinstance(sha256, str) or len(sha256) != 64 or not set(sha256) <= _HEX:
                raise ValueError(f"stream {name}: sha256 must be 64 lowercase hex digits")
            digests.append((name, StreamDigest(count, sha256)))
        manifest = cls(config_sha256, tuple(digests))
        if manifest.to_bytes() != data:
            raise ValueError("manifest bytes are not in canonical form")
        return manifest

    def run_sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


class StreamHasher:
    """Feeds each tick's canonical lines into the digest of the stream its table names."""

    def __init__(self, keep_lines: bool = False) -> None:
        self._hashes = {name: hashlib.sha256() for name in STREAMS}
        self._counts = dict.fromkeys(STREAMS, 0)
        self._lines: dict[str, list[bytes]] | None = (
            {name: [] for name in STREAMS} if keep_lines else None
        )

    def add_tick(self, tick: Tick) -> None:
        for op in tick.ops:
            data = canon.line(tick.seq, op)
            name = op.table.value
            self._hashes[name].update(data)
            self._counts[name] += 1
            if self._lines is not None:
                self._lines[name].append(data)

    def lines(self, name: str) -> list[bytes]:
        """The kept lines of a stream; for tests only (the hasher must be built with keep_lines)."""
        if self._lines is None:
            raise ValueError("this hasher keeps no lines")
        return list(self._lines[name])

    def manifest(self, config_sha256: str) -> Manifest:
        return Manifest(
            config_sha256,
            tuple(
                (name, StreamDigest(self._counts[name], self._hashes[name].hexdigest()))
                for name in STREAMS
            ),
        )


def mismatch_report(expected: Manifest, actual: Manifest) -> str:
    """One line per stream in the fixed order, with both counts and digests and no record."""
    rows = [
        f"run_sha256 expected {expected.run_sha256()} actual {actual.run_sha256()}",
    ]
    for name in STREAMS:
        want, got = expected.digest(name), actual.digest(name)
        rows.append(f"{name} expected {want.count} {want.sha256} actual {got.count} {got.sha256}")
    return "\n".join(rows)
