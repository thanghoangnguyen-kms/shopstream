"""install_gitleaks: the pinned, checksum-verified installer behind `just tools`."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import install_gitleaks as ig
import pytest

VERSION = "8.30.1"


def make_archive(payload: bytes = b"#!/bin/sh\necho 8.30.1\n", name: str = "gitleaks") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def checksums_for(filename: str, data: bytes) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {filename}\n0000  gitleaks_{VERSION}_windows_x64.zip\n"


def fake_binary(dest: Path, version_output: str) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    binary = dest / "gitleaks"
    binary.write_text(f"#!/bin/sh\necho {version_output}\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", f"gitleaks_{VERSION}_darwin_arm64.tar.gz"),
        ("Linux", "x86_64", f"gitleaks_{VERSION}_linux_x64.tar.gz"),
        ("Linux", "aarch64", f"gitleaks_{VERSION}_linux_arm64.tar.gz"),
    ],
)
def test_asset_name(system: str, machine: str, expected: str) -> None:
    assert ig.asset_name(VERSION, system, machine) == expected


def test_asset_name_rejects_unsupported_platform() -> None:
    with pytest.raises(ig.UnsupportedPlatformError, match="Windows"):
        ig.asset_name(VERSION, "Windows", "AMD64")


def test_verify_accepts_matching_checksum() -> None:
    data = make_archive()
    ig.verify(data, checksums_for("a.tar.gz", data), "a.tar.gz")


def test_verify_rejects_mismatch() -> None:
    data = make_archive()
    with pytest.raises(ig.ChecksumError, match="mismatch"):
        ig.verify(data + b"tampered", checksums_for("a.tar.gz", data), "a.tar.gz")


def test_verify_rejects_asset_missing_from_checksums() -> None:
    with pytest.raises(ig.ChecksumError, match="not listed"):
        ig.verify(b"data", "abc123  some_other_asset.tar.gz\n", "a.tar.gz")


def test_extract_binary_writes_an_executable(tmp_path: Path) -> None:
    binary = ig.extract_binary(make_archive(b"payload"), tmp_path)
    assert binary == tmp_path / "gitleaks"
    assert binary.read_bytes() == b"payload"
    assert binary.stat().st_mode & 0o111


def test_extract_binary_requires_the_gitleaks_member(tmp_path: Path) -> None:
    with pytest.raises(ig.ArchiveError, match="gitleaks"):
        ig.extract_binary(make_archive(b"# readme", name="README.md"), tmp_path)


def test_install_skips_download_when_pinned_version_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_binary(tmp_path, VERSION)

    def no_download(url: str) -> bytes:
        raise AssertionError(f"unexpected download: {url}")

    monkeypatch.setattr(ig, "fetch", no_download)
    assert ig.install(VERSION, tmp_path) == tmp_path / "gitleaks"


def test_install_replaces_stale_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_binary(tmp_path, "v8.29.0")
    name = ig.asset_name(VERSION, *ig.current_platform())
    new_payload = f"#!/bin/sh\necho {VERSION}\n".encode()
    archive = make_archive(new_payload)
    base = f"{ig.RELEASE_BASE}/v{VERSION}"
    responses = {
        f"{base}/gitleaks_{VERSION}_checksums.txt": checksums_for(name, archive).encode(),
        f"{base}/{name}": archive,
    }
    monkeypatch.setattr(ig, "fetch", responses.__getitem__)
    binary = ig.install(VERSION, tmp_path)
    assert binary.read_bytes() == new_payload
    assert ig.installed_version(binary) == VERSION


def test_install_refuses_tampered_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = ig.asset_name(VERSION, *ig.current_platform())
    genuine = make_archive()
    base = f"{ig.RELEASE_BASE}/v{VERSION}"
    responses = {
        f"{base}/gitleaks_{VERSION}_checksums.txt": checksums_for(name, genuine).encode(),
        f"{base}/{name}": make_archive(b"#!/bin/sh\necho evil\n"),
    }
    monkeypatch.setattr(ig, "fetch", responses.__getitem__)
    with pytest.raises(ig.ChecksumError, match="mismatch"):
        ig.install(VERSION, tmp_path)
    assert not (tmp_path / "gitleaks").exists()
