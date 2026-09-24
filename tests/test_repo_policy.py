"""Repo policy: every container image in a Compose file is pinned by digest.

Passes trivially until Week 2 adds infra/compose.yaml; from then on an image referenced
by tag alone fails the build (Renovate's docker:pinDigests keeps the digests current).
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".tools",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "target",
    }
)
COMPOSE_NAMES = ("compose.y*ml", "compose.*.y*ml", "docker-compose.y*ml", "docker-compose.*.y*ml")


def compose_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS]
        found.extend(
            Path(directory) / name
            for name in files
            if any(fnmatch.fnmatch(name, pattern) for pattern in COMPOSE_NAMES)
        )
    return sorted(found)


def unpinned_images(root: Path) -> list[str]:
    violations: list[str] = []
    for path in compose_files(root):
        services = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("services") or {}
        for name, service in services.items():
            image = (service or {}).get("image")
            if image is not None and "@sha256:" not in str(image):
                violations.append(
                    f"{path.relative_to(root)}: service {name} image {image} is not pinned by digest"
                )
    return violations


def write_compose(root: Path, text: str, relative: str = "infra/compose.yaml") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_repo_images_are_pinned() -> None:
    assert unpinned_images(REPO) == []


def test_flags_a_tag_only_image(tmp_path: Path) -> None:
    write_compose(tmp_path, "services:\n  postgres:\n    image: postgres:17\n")
    assert unpinned_images(tmp_path) == [
        "infra/compose.yaml: service postgres image postgres:17 is not pinned by digest"
    ]


def test_accepts_a_digest_pinned_image(tmp_path: Path) -> None:
    write_compose(
        tmp_path, "services:\n  postgres:\n    image: postgres:17@sha256:" + "a" * 64 + "\n"
    )
    assert unpinned_images(tmp_path) == []


def test_ignores_build_only_services(tmp_path: Path) -> None:
    write_compose(tmp_path, "services:\n  spark:\n    build: ./spark\n")
    assert unpinned_images(tmp_path) == []


def test_finds_profile_overrides(tmp_path: Path) -> None:
    write_compose(
        tmp_path,
        "services:\n  kafka:\n    image: apache/kafka:4.3.1\n",
        "infra/compose.streaming.yaml",
    )
    assert len(unpinned_images(tmp_path)) == 1
