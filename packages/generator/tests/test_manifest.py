"""The manifest: per-stream digests in emission order, canonical bytes, a record-free report."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from shopstream_generator import canon
from shopstream_generator.manifest import (
    EMPTY_SHA256,
    Manifest,
    StreamHasher,
    mismatch_report,
)
from shopstream_generator.ops import STREAMS, Op, OpKind, Table, Tick

REPO = Path(__file__).resolve().parents[3]
GOLDEN = REPO / "packages/generator/tests/golden/manifest.json"
CONFIG_SHA = "0" * 64
STAMP = datetime(2025, 7, 1, tzinfo=UTC)


def insert(customer_id: int) -> Op:
    return Op(
        Table.CUSTOMERS,
        OpKind.INSERT,
        {"customer_id": customer_id},
        {"customer_id": customer_id, "created_at": STAMP, "deleted_at": None},
    )


def tick(seq: int, *ops: Op) -> Tick:
    return Tick(seq, seq, tuple(ops))


def test_the_empty_digest_is_the_sha256_of_nothing() -> None:
    assert EMPTY_SHA256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_a_hasher_with_no_ticks_lists_seven_empty_streams() -> None:
    manifest = StreamHasher().manifest(CONFIG_SHA)
    assert tuple(name for name, _ in manifest.streams) == STREAMS
    assert len(manifest.streams) == 7
    assert all(d.count == 0 and d.sha256 == EMPTY_SHA256 for _, d in manifest.streams)


def test_each_line_feeds_only_its_own_stream() -> None:
    hasher = StreamHasher(keep_lines=True)
    hasher.add_tick(tick(1, insert(1)))
    manifest = hasher.manifest(CONFIG_SHA)
    assert manifest.digest("customers").count == 1
    assert (
        manifest.digest("customers").sha256 == hashlib.sha256(canon.line(1, insert(1))).hexdigest()
    )
    assert manifest.digest("orders").sha256 == EMPTY_SHA256
    assert hasher.lines("customers") == [canon.line(1, insert(1))]
    assert hasher.lines("orders") == []


def test_swapping_two_lines_changes_the_digest() -> None:
    forward, backward = StreamHasher(), StreamHasher()
    forward.add_tick(tick(1, insert(1), insert(2)))
    backward.add_tick(tick(1, insert(2), insert(1)))
    assert forward.manifest(CONFIG_SHA).digest("customers") != backward.manifest(CONFIG_SHA).digest(
        "customers"
    )


def test_a_hasher_that_keeps_no_lines_refuses_to_return_them() -> None:
    with pytest.raises(ValueError, match="keeps no lines"):
        StreamHasher().lines("customers")


def test_the_bytes_end_in_one_newline_and_round_trip() -> None:
    hasher = StreamHasher()
    hasher.add_tick(tick(1, insert(1)))
    data = hasher.manifest(CONFIG_SHA).to_bytes()
    assert data.endswith(b"}\n")
    assert data.count(b"\n") == 1
    assert Manifest.from_bytes(data).to_bytes() == data


def test_the_run_hash_is_the_sha256_of_the_file_bytes() -> None:
    manifest = StreamHasher().manifest(CONFIG_SHA)
    assert manifest.run_sha256() == hashlib.sha256(manifest.to_bytes()).hexdigest()


def _body(**changes: object) -> bytes:
    streams = {name: {"count": 0, "sha256": EMPTY_SHA256} for name in STREAMS}
    body: dict[str, object] = {"config_sha256": CONFIG_SHA, "format": 1, "streams": streams}
    body.update(changes)
    return canon.dumps(body).encode() + b"\n"


def test_the_helper_builds_a_valid_manifest() -> None:
    assert Manifest.from_bytes(_body()).to_bytes() == _body()


def test_from_bytes_rejects_another_format() -> None:
    with pytest.raises(ValueError, match="format"):
        Manifest.from_bytes(_body(format=2))


def test_from_bytes_rejects_a_missing_stream() -> None:
    streams = {name: {"count": 0, "sha256": EMPTY_SHA256} for name in STREAMS[:-1]}
    with pytest.raises(ValueError, match="seven"):
        Manifest.from_bytes(_body(streams=streams))


def test_from_bytes_rejects_an_extra_stream() -> None:
    streams = {name: {"count": 0, "sha256": EMPTY_SHA256} for name in (*STREAMS, "extra")}
    with pytest.raises(ValueError, match="seven"):
        Manifest.from_bytes(_body(streams=streams))


def test_from_bytes_rejects_a_missing_trailing_newline_and_extra_whitespace() -> None:
    with pytest.raises(ValueError, match="canonical"):
        Manifest.from_bytes(_body().rstrip(b"\n"))
    with pytest.raises(ValueError, match="canonical"):
        Manifest.from_bytes(_body() + b"\n")


def test_from_bytes_rejects_a_float_count() -> None:
    streams = {name: {"count": 0, "sha256": EMPTY_SHA256} for name in STREAMS}
    raw = _body(streams=streams).replace(b'"count":0', b'"count":0.5', 1)
    with pytest.raises(ValueError, match="float"):
        Manifest.from_bytes(raw)


def test_the_mismatch_report_lists_every_stream_and_both_digests_but_no_record() -> None:
    left, right = StreamHasher(), StreamHasher()
    left.add_tick(tick(1, insert(1)))
    right.add_tick(tick(1, insert(2)))
    expected, actual = left.manifest(CONFIG_SHA), right.manifest(CONFIG_SHA)
    report = mismatch_report(expected, actual)
    lines = report.splitlines()
    assert [line.split()[0] for line in lines[1:]] == list(STREAMS)
    for name, digest in expected.streams:
        assert name in report
        assert digest.sha256 in report
    for _, digest in actual.streams:
        assert digest.sha256 in report
    assert expected.run_sha256() in report
    assert '{"seq"' not in report
    assert "customer_id" not in report


def test_the_committed_golden_is_canonical() -> None:
    data = GOLDEN.read_bytes()
    assert Manifest.from_bytes(data).to_bytes() == data
