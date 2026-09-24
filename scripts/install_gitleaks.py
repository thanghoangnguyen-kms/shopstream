"""Install the pinned gitleaks release into .tools/bin, verified against its checksums.

`just tools` calls this with the version pinned in the justfile. gitleaks is a Go binary,
so uv.lock can't pin it. This script is the one place that downloads it; the pre-commit
hook, CI and tests/test_secret_gate.py all run the binary it installs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

RELEASE_BASE = "https://github.com/gitleaks/gitleaks/releases/download"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO_ROOT / ".tools" / "bin"
_OS = {"Darwin": "darwin", "Linux": "linux"}
_ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}


class UnsupportedPlatformError(RuntimeError):
    """No gitleaks release asset exists for this OS/architecture pair."""


class ChecksumError(RuntimeError):
    """The downloaded archive doesn't match the release's published checksum."""


class ArchiveError(RuntimeError):
    """The release archive doesn't contain the gitleaks binary."""


def current_platform() -> tuple[str, str]:
    return platform.system(), platform.machine()


def asset_name(version: str, system: str, machine: str) -> str:
    try:
        os_part, arch_part = _OS[system], _ARCH[machine]
    except KeyError as exc:
        raise UnsupportedPlatformError(f"no gitleaks asset for {system}/{machine}") from exc
    return f"gitleaks_{version}_{os_part}_{arch_part}.tar.gz"


def expected_sha256(checksums: str, filename: str) -> str:
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == filename:
            return parts[0].lower()
    raise ChecksumError(f"{filename} is not listed in the release checksums")


def verify(data: bytes, checksums: str, filename: str) -> None:
    expected = expected_sha256(checksums, filename)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ChecksumError(f"checksum mismatch for {filename}: expected {expected}, got {actual}")


def installed_version(binary: Path) -> str | None:
    if not binary.is_file():
        return None
    # Fixed argv; the binary is the one this script installed under .tools/bin.
    result = subprocess.run(  # noqa: S603
        [str(binary), "version"], capture_output=True, text=True, check=False
    )
    version = result.stdout.strip().removeprefix("v")
    return version or None


def extract_binary(archive: bytes, dest: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        try:
            member = tar.getmember("gitleaks")
        except KeyError as exc:
            raise ArchiveError("release archive has no 'gitleaks' binary") from exc
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ArchiveError("'gitleaks' in the release archive is not a regular file")
        payload = extracted.read()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "gitleaks"
    target.write_bytes(payload)
    target.chmod(0o755)
    return target


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    # https is enforced above, so file: and custom schemes can't reach urlopen.
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        body: bytes = response.read()
    return body


def install(version: str, dest: Path = DEFAULT_DEST) -> Path:
    binary = dest / "gitleaks"
    if installed_version(binary) == version:
        return binary
    name = asset_name(version, *current_platform())
    base = f"{RELEASE_BASE}/v{version}"
    checksums = fetch(f"{base}/gitleaks_{version}_checksums.txt").decode("utf-8")
    archive = fetch(f"{base}/{name}")
    verify(archive, checksums, name)
    return extract_binary(archive, dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="gitleaks version without the leading v, e.g. 8.30.1")
    args = parser.parse_args(argv)
    print(install(args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
