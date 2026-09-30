"""Repo policy gate (GATE-01) over every Compose file and Dockerfile the repo lists.

Files are found with `git ls-files --cached --others --exclude-standard`, so an untracked
`infra/compose.*.yaml` is judged the moment it exists while a git-ignored copy never is.
The gate enforces: images and Dockerfile bases pinned by an anchored sha256 digest, ports
published on literal 127.0.0.1 only, a literal `mem_limit` equal to `memswap_limit`, a
healthcheck on every long-running service, `restart: "no"` on every one-shot, no `include:`
or `extends:` (each file is read on its own), and the generated identity file and memory
samples kept out of git (D-08, D-15, D-16). Renovate's docker:pinDigests keeps digests fresh.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import stack
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
        "dbt_packages",
        "logs",
        "__pycache__",
    }
)
COMPOSE_NAMES = ("compose.y*ml", "compose.*.y*ml", "docker-compose.y*ml", "docker-compose.*.y*ml")
DOCKERFILE_NAMES = ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "Containerfile")

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
MEM = re.compile(r"\d+(?:[bkmg]|kb|mb|gb)?", re.IGNORECASE)
MEM_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2}
MEM_UNITS |= {"g": 1024**3, "gb": 1024**3}
LOOPBACK_PORT = re.compile(
    r"127\.0\.0\.1:(?P<host>\d+(?:-\d+)?):(?P<container>\d+(?:-\d+)?)(?:/(?:tcp|udp|sctp))?"
)
PORT_RANGE = re.compile(r"(\d+)(?:-(\d+))?")
FROM_LINE = re.compile(
    r"FROM\s+(?:--\S+\s+)*(?P<ref>\S+)(?:\s+AS\s+(?P<alias>\S+))?", re.IGNORECASE
)
COPY_FROM = re.compile(r"COPY\s+.*?--from=(?P<ref>\S+)", re.IGNORECASE)
IGNORED_PATHS = (
    "infra/.generated/seaweedfs/iam.json",
    ".mem/samples.jsonl",
    "infra/.env",
    "infra/.env.a1b2c3d4.tmp",
)


def _git_env() -> dict[str, str]:
    """Child environment without GIT_* so a run inside a git hook never redirects git."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], check=check, capture_output=True, cwd=root, env=_git_env()
    )


def git_listed(root: Path) -> list[Path]:
    """Tracked plus untracked-not-ignored files, relative to root, that exist on disk."""
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    names = [name for name in out.stdout.decode("utf-8").split("\0") if name]
    # git still lists a file that was staged and then deleted, so require it to exist.
    return sorted(
        Path(name)
        for name in names
        if (root / name).is_file() and not SKIP_DIRS.intersection(Path(name).parts)
    )


def compose_files(root: Path) -> list[Path]:
    return [
        path
        for path in git_listed(root)
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in COMPOSE_NAMES)
    ]


def dockerfiles(root: Path) -> list[Path]:
    return [
        path
        for path in git_listed(root)
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in DOCKERFILE_NAMES)
    ]


def load_document(root: Path, rel: Path) -> dict[str, Any]:
    data = yaml.safe_load((root / rel).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_services(root: Path, rel: Path) -> dict[str, dict[str, Any]]:
    services = load_document(root, rel).get("services")
    if not isinstance(services, dict):
        return {}
    return {str(n): spec if isinstance(spec, dict) else {} for n, spec in services.items()}


def unpinned_images(root: Path) -> list[str]:
    violations: list[str] = []
    for rel in compose_files(root):
        for name, service in load_services(root, rel).items():
            image = service.get("image")
            if image is not None and not DIGEST.search(str(image)):
                violations.append(f"{rel}: service {name} image {image} is not pinned by digest")
    return sorted(violations)


def logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Dockerfile instructions with backslash continuations joined and comments dropped."""
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not buffer:
            start = number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer = (buffer + line).strip()
        if buffer:
            yield start, buffer
        buffer = ""
    if buffer.strip():
        yield start, buffer.strip()


def unpinned_from_lines(root: Path) -> list[str]:
    violations: list[str] = []
    for rel in dockerfiles(root):
        aliases: set[str] = set()
        text = (root / rel).read_text(encoding="utf-8")
        for number, line in logical_lines(text):
            where = f"{rel}:{number}"
            if from_match := FROM_LINE.fullmatch(line):
                ref = from_match["ref"]
                if "$" in ref:
                    violations.append(f"{where}: FROM {ref} is ARG-driven")
                elif (
                    ref.lower() != "scratch"
                    and ref.lower() not in aliases
                    and not DIGEST.search(ref)
                ):
                    violations.append(f"{where}: FROM {ref} is not pinned by digest")
                if from_match["alias"]:
                    aliases.add(from_match["alias"].lower())
            elif copy_match := COPY_FROM.match(line):
                ref = copy_match["ref"]
                if not (ref.isdecimal() or ref.lower() in aliases or DIGEST.search(ref)):
                    violations.append(f"{where}: COPY --from={ref} is not pinned by digest")
    return sorted(violations)


def _expand_ports(spec: object) -> list[int]:
    match = PORT_RANGE.fullmatch(str(spec))
    if not match:
        return []
    return list(range(int(match[1]), int(match[2] or match[1]) + 1))


def unsafe_ports(root: Path) -> list[str]:
    violations: list[str] = []
    for rel in compose_files(root):
        owners: dict[int, set[str]] = {}
        for name, service in load_services(root, rel).items():
            if str(service.get("network_mode", "")) == "host":
                violations.append(f"{rel}: service {name} uses network_mode host")
            for entry in service.get("ports") or []:
                if isinstance(entry, dict):
                    if entry.get("host_ip") != "127.0.0.1" or "published" not in entry:
                        violations.append(
                            f"{rel}: service {name} long-syntax port is not bound to 127.0.0.1"
                        )
                    else:
                        for port in _expand_ports(entry["published"]):
                            owners.setdefault(port, set()).add(name)
                    continue
                binding = LOOPBACK_PORT.fullmatch(entry) if isinstance(entry, str) else None
                if binding is None:
                    violations.append(
                        f"{rel}: service {name} port {entry!r} is not a literal "
                        "127.0.0.1:host:container binding"
                    )
                    continue
                for port in _expand_ports(binding["host"]):
                    owners.setdefault(port, set()).add(name)
        violations.extend(
            f"{rel}: host port {port} is published by more than one service"
            for port, names in owners.items()
            if len(names) > 1
        )
    return sorted(violations)


def mem_bytes(value: object) -> int | None:
    """Bytes for a literal mem_limit (a YAML int or `512m` style string), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and MEM.fullmatch(value):
        digits = re.match(r"\d+", value)
        assert digits is not None
        return int(digits[0]) * MEM_UNITS[value[digits.end() :].lower()]
    return None


def memory_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for rel in compose_files(root):
        for name, service in load_services(root, rel).items():
            limit = mem_bytes(service.get("mem_limit"))
            if limit is None:
                violations.append(f"{rel}: service {name} has no literal mem_limit")
            elif mem_bytes(service.get("memswap_limit")) != limit:
                violations.append(f"{rel}: service {name} memswap_limit differs from mem_limit")
            deploy = service.get("deploy")
            resources = deploy.get("resources") if isinstance(deploy, dict) else None
            limits = resources.get("limits") if isinstance(resources, dict) else None
            if isinstance(limits, dict) and "memory" in limits:
                violations.append(f"{rel}: service {name} sets deploy.resources.limits.memory")
    return sorted(violations)


def _one_shots(services: dict[str, dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for name, service in services.items():
        profiles = service.get("profiles")
        if isinstance(profiles, list) and "bootstrap" in profiles:
            names.add(name)
        depends = service.get("depends_on")
        if isinstance(depends, dict):
            names.update(
                str(dep)
                for dep, cond in depends.items()
                if isinstance(cond, dict)
                and cond.get("condition") == "service_completed_successfully"
            )
    return names


def _has_healthcheck(service: dict[str, Any]) -> bool:
    check = service.get("healthcheck")
    if not isinstance(check, dict) or not check.get("test") or check.get("disable") is True:
        return False
    return bool(check["test"] != ["NONE"])


def healthcheck_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for rel in compose_files(root):
        services = load_services(root, rel)
        one_shots = _one_shots(services)
        for name, service in services.items():
            if name in one_shots:
                # PyYAML reads an unquoted `restart: no` as False, which is reported too.
                if service.get("restart") != "no":
                    violations.append(
                        f'{rel}: service {name} is a one-shot but restart is not "no"'
                    )
            elif not _has_healthcheck(service):
                violations.append(f"{rel}: service {name} is long-running but has no healthcheck")
    return sorted(violations)


def structure_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for rel in compose_files(root):
        if "include" in load_document(root, rel):
            violations.append(f"{rel}: top-level include is not allowed (files are checked alone)")
        violations.extend(
            f"{rel}: service {name} uses extends"
            for name, service in load_services(root, rel).items()
            if "extends" in service
        )
    return sorted(violations)


def generated_tracked(root: Path) -> list[str]:
    out = _git(root, "ls-files", "-z", "--cached", "--", "infra/.generated")
    names = [name for name in out.stdout.decode("utf-8").split("\0") if name]
    return sorted(f"{name}: generated file is tracked by git" for name in names)


def ignore_violations(root: Path) -> list[str]:
    return sorted(
        f"{path} is not git-ignored"
        for path in IGNORED_PATHS
        if _git(root, "check-ignore", "-q", path, check=False).returncode != 0
    )


CHECKS: dict[str, Callable[[Path], list[str]]] = {
    "images": unpinned_images,
    "from-lines": unpinned_from_lines,
    "ports": unsafe_ports,
    "memory": memory_violations,
    "healthchecks": healthcheck_violations,
    "structure": structure_violations,
    "generated-tracked": generated_tracked,
    "ignore": ignore_violations,
}


def write_compose(root: Path, text: str, relative: str = "infra/compose.yaml") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def write_service(root: Path, body: str, name: str = "web") -> None:
    """One service `name` whose body is given as 4-space-indented YAML lines."""
    write_compose(root, f"services:\n  {name}:\n{body}")


HEX = "a" * 64
GOOD_BODY = (
    f"    image: web:1@sha256:{HEX}\n"
    "    mem_limit: 256m\n"
    "    memswap_limit: 256m\n"
    "    healthcheck:\n"
    "      test: [CMD, true]\n"
)


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_repo_passes(name: str) -> None:
    assert CHECKS[name](REPO) == []


def test_an_untracked_compose_file_is_judged(git_repo: Path) -> None:
    write_compose(git_repo, "services:\n  x:\n    image: x:1\n", "infra/compose.streaming.yaml")
    assert compose_files(git_repo) == [Path("infra/compose.streaming.yaml")]


def test_a_git_ignored_compose_file_is_never_scanned(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("infra/.generated/\n", encoding="utf-8")
    write_compose(git_repo, "services:\n  x:\n    image: x:1\n", "infra/.generated/compose.yaml")
    assert compose_files(git_repo) == []
    assert unpinned_images(git_repo) == []


def test_skips_generated_directories(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("dbt_packages/\nlogs/\n__pycache__/\n", encoding="utf-8")
    for generated in ("dbt_packages/some_pkg", "logs", "__pycache__"):
        write_compose(
            git_repo, "services:\n  x:\n    image: x:1\n", f"{generated}/docker-compose.yml"
        )
    assert compose_files(git_repo) == []
    assert unpinned_images(git_repo) == []


def test_skip_dirs_apply_even_when_git_lists_the_file(git_repo: Path) -> None:
    write_compose(git_repo, "services:\n  x:\n    image: x:1\n", "node_modules/p/compose.yaml")
    assert compose_files(git_repo) == []


def test_a_staged_then_deleted_file_does_not_crash(git_repo: Path) -> None:
    write_compose(git_repo, "services:\n  x:\n    image: x:1\n")
    _git(git_repo, "add", "infra/compose.yaml")
    (git_repo / "infra/compose.yaml").unlink()
    assert git_listed(git_repo) == []
    assert all(check(git_repo) == [] or name == "ignore" for name, check in CHECKS.items())


def test_git_listing_is_sorted(git_repo: Path) -> None:
    for relative in ("infra/compose.b.yaml", "infra/compose.a.yaml", "compose.yaml"):
        write_compose(git_repo, "services: {}\n", relative)
    assert compose_files(git_repo) == [
        Path("compose.yaml"),
        Path("infra/compose.a.yaml"),
        Path("infra/compose.b.yaml"),
    ]


def test_an_empty_repo_has_no_violations(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text(
        "infra/.generated/\n.mem/\ninfra/.env\ninfra/.env.*\n", encoding="utf-8"
    )
    for name, check in CHECKS.items():
        assert check(git_repo) == [], name


def test_a_missing_ignore_rule_is_reported(git_repo: Path) -> None:
    assert ignore_violations(git_repo) == [
        ".mem/samples.jsonl is not git-ignored",
        "infra/.env is not git-ignored",
        "infra/.env.a1b2c3d4.tmp is not git-ignored",
        "infra/.generated/seaweedfs/iam.json is not git-ignored",
    ]


def test_a_tracked_generated_file_is_reported(git_repo: Path) -> None:
    write_compose(git_repo, "{}\n", "infra/.generated/seaweedfs/iam.json")
    _git(git_repo, "add", "-f", "infra/.generated/seaweedfs/iam.json")
    assert generated_tracked(git_repo) == [
        "infra/.generated/seaweedfs/iam.json: generated file is tracked by git"
    ]


def test_an_untracked_generated_file_is_not_reported(git_repo: Path) -> None:
    write_compose(git_repo, "{}\n", "infra/.generated/seaweedfs/iam.json")
    assert generated_tracked(git_repo) == []


# Images


def test_flags_a_tag_only_image(git_repo: Path) -> None:
    write_compose(git_repo, "services:\n  postgres:\n    image: postgres:17\n")
    assert unpinned_images(git_repo) == [
        "infra/compose.yaml: service postgres image postgres:17 is not pinned by digest"
    ]


def test_accepts_a_digest_pinned_image(git_repo: Path) -> None:
    write_compose(git_repo, f"services:\n  postgres:\n    image: postgres:17@sha256:{HEX}\n")
    assert unpinned_images(git_repo) == []


@pytest.mark.parametrize(
    "digest",
    ["${DIGEST}", "a" * 63, "a" * 65, "A" * 64, "abc123", "a" * 63 + "g"],
    ids=["interpolated", "63-hex", "65-hex", "uppercase", "short", "non-hex"],
)
def test_flags_a_malformed_digest(git_repo: Path, digest: str) -> None:
    image = f"postgres:17@sha256:{digest}"
    write_compose(git_repo, f"services:\n  postgres:\n    image: {image}\n")
    assert unpinned_images(git_repo) == [
        f"infra/compose.yaml: service postgres image {image} is not pinned by digest"
    ]


def test_ignores_build_only_services(git_repo: Path) -> None:
    write_compose(git_repo, "services:\n  spark:\n    build: ./spark\n")
    assert unpinned_images(git_repo) == []
    assert unsafe_ports(git_repo) == []


def test_image_messages_are_ordered_by_file_then_service(git_repo: Path) -> None:
    write_compose(git_repo, "services:\n  c:\n    image: c:1\n", "compose.yaml")
    write_compose(
        git_repo, "services:\n  b:\n    image: b:1\n  a:\n    image: a:1\n", "compose.y.yml"
    )
    assert [message.split(" image ")[0] for message in unpinned_images(git_repo)] == [
        "compose.y.yml: service a",
        "compose.y.yml: service b",
        "compose.yaml: service c",
    ]


# Dockerfiles


def dockerfile(root: Path, text: str) -> None:
    write_compose(root, text, "infra/Dockerfile")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("FROM python:3.13\n", "infra/Dockerfile:1: FROM python:3.13 is not pinned by digest"),
        (
            "FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS builder\n",
            "infra/Dockerfile:1: FROM golang:1.26-alpine is not pinned by digest",
        ),
        (
            "ARG BASE=x\nFROM ${BASE}\n",
            "infra/Dockerfile:2: FROM ${BASE} is ARG-driven",
        ),
        (
            f"FROM --platform=linux/amd64 golang:1@sha256:{'a' * 63}\n",
            f"infra/Dockerfile:1: FROM golang:1@sha256:{'a' * 63} is not pinned by digest",
        ),
        (
            "from python:3.13 as Base\n",
            "infra/Dockerfile:1: FROM python:3.13 is not pinned by digest",
        ),
        (
            f"FROM python:3.13@sha256:{HEX}\nCOPY --from=nginx:1 /a /b\n",
            "infra/Dockerfile:2: COPY --from=nginx:1 is not pinned by digest",
        ),
        (
            "FROM \\\n  python:3.13 \\\n  AS base\n",
            "infra/Dockerfile:1: FROM python:3.13 is not pinned by digest",
        ),
    ],
    ids=[
        "tag",
        "platform-without-digest",
        "arg-driven",
        "short-digest",
        "lowercase-instruction",
        "copy-from-image",
        "continuation",
    ],
)
def test_flags_an_unpinned_dockerfile_line(git_repo: Path, text: str, message: str) -> None:
    dockerfile(git_repo, text)
    assert unpinned_from_lines(git_repo) == [message]


@pytest.mark.parametrize(
    "text",
    [
        f"FROM --platform=linux/amd64 golang:1@sha256:{HEX} AS builder\n",
        f"FROM golang:1@sha256:{HEX} AS builder\nFROM builder\n",
        f"FROM golang:1@sha256:{HEX} AS Builder\nFROM builder AS again\n",
        "FROM scratch\nCOPY x /x\n",
        f"FROM golang:1@sha256:{HEX} AS build\nFROM scratch\nCOPY --from=build /a /b\nCOPY --from=0 /a /c\n",
        f"FROM golang:1@sha256:{HEX}\nCOPY --from=nginx:1@sha256:{HEX} /a /b\n",
        "# FROM python:3.13\nFROM scratch\n",
        "",
    ],
    ids=[
        "platform-with-digest",
        "earlier-alias",
        "alias-case-insensitive",
        "scratch",
        "copy-from-alias-and-index",
        "copy-from-digest",
        "comment",
        "empty-file",
    ],
)
def test_accepts_a_pinned_dockerfile(git_repo: Path, text: str) -> None:
    dockerfile(git_repo, text)
    assert unpinned_from_lines(git_repo) == []


@pytest.mark.parametrize(
    "name", ["Dockerfile", "Dockerfile.seaweed", "seaweed.Dockerfile", "Containerfile"]
)
def test_dockerfile_names_are_found(git_repo: Path, name: str) -> None:
    write_compose(git_repo, "FROM python:3.13\n", f"images/{name}")
    assert len(unpinned_from_lines(git_repo)) == 1


# Ports


def ports(root: Path, *entries: str) -> list[str]:
    body = "    ports:\n" + "".join(f"      - {entry}\n" for entry in entries)
    write_service(root, body)
    return unsafe_ports(root)


def binding_message(entry: object) -> str:
    return (
        f"infra/compose.yaml: service web port {entry!r} is not a literal "
        "127.0.0.1:host:container binding"
    )


@pytest.mark.parametrize(
    ("entry", "parsed"),
    [
        ('"8181:8181"', "8181:8181"),
        ('"0.0.0.0:80:80"', "0.0.0.0:80:80"),
        ('"[::]:8181:8181"', "[::]:8181:8181"),
        ('"127.0.0.1:${P}:80"', "127.0.0.1:${P}:80"),
        ('"127.0.0.1:80"', "127.0.0.1:80"),
        ('"80"', "80"),
        ("8090", 8090),
        ("22:22", 1342),
    ],
    ids=[
        "short",
        "all-interfaces",
        "ipv6-any",
        "interpolated",
        "no-host-port",
        "bare-string",
        "int",
        "yaml-sexagesimal",
    ],
)
def test_flags_an_unsafe_port(git_repo: Path, entry: str, parsed: object) -> None:
    assert ports(git_repo, entry) == [binding_message(parsed)]


@pytest.mark.parametrize(
    "entry", ['"127.0.0.1:8181:8181"', '"127.0.0.1:8100-8102:80-82"', '"127.0.0.1:53:53/udp"']
)
def test_accepts_a_loopback_port(git_repo: Path, entry: str) -> None:
    assert ports(git_repo, entry) == []


def test_flags_a_long_syntax_port_without_loopback(git_repo: Path) -> None:
    for extra in ("", "        host_ip: 0.0.0.0\n"):
        write_service(git_repo, "    ports:\n      - target: 80\n        published: 8080\n" + extra)
        assert unsafe_ports(git_repo) == [
            "infra/compose.yaml: service web long-syntax port is not bound to 127.0.0.1"
        ]


def test_flags_a_long_syntax_port_without_published(git_repo: Path) -> None:
    write_service(git_repo, "    ports:\n      - target: 80\n        host_ip: 127.0.0.1\n")
    assert len(unsafe_ports(git_repo)) == 1


def test_accepts_a_long_syntax_loopback_port(git_repo: Path) -> None:
    write_service(
        git_repo,
        "    ports:\n      - target: 80\n        published: 8080\n        host_ip: 127.0.0.1\n",
    )
    assert unsafe_ports(git_repo) == []


def test_flags_network_mode_host(git_repo: Path) -> None:
    write_service(git_repo, "    network_mode: host\n")
    assert unsafe_ports(git_repo) == ["infra/compose.yaml: service web uses network_mode host"]


def test_flags_a_host_port_published_twice(git_repo: Path) -> None:
    write_compose(
        git_repo,
        'services:\n  a:\n    ports: ["127.0.0.1:8181:8181"]\n  b:\n    ports: ["127.0.0.1:8181:80"]\n',
    )
    assert unsafe_ports(git_repo) == [
        "infra/compose.yaml: host port 8181 is published by more than one service"
    ]


def test_flags_overlapping_port_ranges(git_repo: Path) -> None:
    write_compose(
        git_repo,
        'services:\n  a:\n    ports: ["127.0.0.1:8100-8102:8100-8102"]\n'
        '  b:\n    ports: ["127.0.0.1:8102:80"]\n',
    )
    assert unsafe_ports(git_repo) == [
        "infra/compose.yaml: host port 8102 is published by more than one service"
    ]


def test_distinct_host_ports_pass(git_repo: Path) -> None:
    write_compose(
        git_repo,
        'services:\n  a:\n    ports: ["127.0.0.1:8181:8181"]\n  b:\n    ports: ["127.0.0.1:8182:8181"]\n',
    )
    assert unsafe_ports(git_repo) == []


# Memory


@pytest.mark.parametrize("limit", ["512m", "512M", "1g", "1GB", "64kb", "1024", "536870912"])
def test_accepts_a_literal_limit(git_repo: Path, limit: str) -> None:
    write_service(git_repo, f"    mem_limit: {limit}\n    memswap_limit: {limit}\n")
    assert memory_violations(git_repo) == []


def test_accepts_equal_limits_in_different_units(git_repo: Path) -> None:
    write_service(git_repo, "    mem_limit: 1g\n    memswap_limit: 1024m\n")
    assert memory_violations(git_repo) == []


def test_flags_a_missing_mem_limit(git_repo: Path) -> None:
    write_service(git_repo, "    image: x\n")
    assert memory_violations(git_repo) == [
        "infra/compose.yaml: service web has no literal mem_limit"
    ]


@pytest.mark.parametrize("limit", ["${MEM}", "${MEM:-512m}", "lots", "1.5g", "-1", "true"])
def test_flags_a_non_literal_mem_limit(git_repo: Path, limit: str) -> None:
    write_service(git_repo, f"    mem_limit: {limit}\n    memswap_limit: {limit}\n")
    assert memory_violations(git_repo) == [
        "infra/compose.yaml: service web has no literal mem_limit"
    ]


@pytest.mark.parametrize("swap", ["1g", "-1", "${MEM}", None])
def test_flags_a_different_or_missing_memswap_limit(git_repo: Path, swap: str | None) -> None:
    swap_line = "" if swap is None else f"    memswap_limit: {swap}\n"
    write_service(git_repo, "    mem_limit: 512m\n" + swap_line)
    assert memory_violations(git_repo) == [
        "infra/compose.yaml: service web memswap_limit differs from mem_limit"
    ]


def test_flags_deploy_resources_memory(git_repo: Path) -> None:
    write_service(
        git_repo,
        "    mem_limit: 512m\n    memswap_limit: 512m\n"
        "    deploy:\n      resources:\n        limits:\n          memory: 512m\n",
    )
    assert memory_violations(git_repo) == [
        "infra/compose.yaml: service web sets deploy.resources.limits.memory"
    ]


def test_deploy_without_a_memory_limit_passes(git_repo: Path) -> None:
    write_service(
        git_repo,
        "    mem_limit: 512m\n    memswap_limit: 512m\n    deploy:\n      replicas: 1\n",
    )
    assert memory_violations(git_repo) == []


# Healthchecks and one-shots


def test_flags_a_long_running_service_without_a_healthcheck(git_repo: Path) -> None:
    write_service(git_repo, "    image: x\n")
    assert healthcheck_violations(git_repo) == [
        "infra/compose.yaml: service web is long-running but has no healthcheck"
    ]


@pytest.mark.parametrize(
    "check",
    [
        "healthcheck:\n      disable: true\n",
        "healthcheck:\n      interval: 5s\n",
        "healthcheck:\n      test: [NONE]\n",
    ],
    ids=["disabled", "no-test", "none-test"],
)
def test_flags_a_healthcheck_that_checks_nothing(git_repo: Path, check: str) -> None:
    write_service(git_repo, f"    {check}")
    assert len(healthcheck_violations(git_repo)) == 1


def test_a_service_with_a_healthcheck_passes(git_repo: Path) -> None:
    write_service(git_repo, GOOD_BODY)
    assert healthcheck_violations(git_repo) == []


def test_a_bootstrap_profile_service_needs_restart_no(git_repo: Path) -> None:
    write_service(git_repo, '    profiles: [bootstrap]\n    restart: "no"\n')
    assert healthcheck_violations(git_repo) == []


def test_an_unquoted_restart_no_is_reported(git_repo: Path) -> None:
    write_service(git_repo, "    profiles: [bootstrap]\n    restart: no\n")
    assert healthcheck_violations(git_repo) == [
        'infra/compose.yaml: service web is a one-shot but restart is not "no"'
    ]


def test_a_one_shot_missing_restart_is_reported(git_repo: Path) -> None:
    write_service(git_repo, "    profiles: [bootstrap]\n")
    assert len(healthcheck_violations(git_repo)) == 1


def test_a_completed_successfully_dependency_marks_a_one_shot(git_repo: Path) -> None:
    write_compose(
        git_repo,
        "services:\n"
        "  app:\n    healthcheck:\n      test: [CMD, true]\n"
        "    depends_on:\n      init:\n        condition: service_completed_successfully\n"
        "  init:\n    image: x\n",
    )
    assert healthcheck_violations(git_repo) == [
        'infra/compose.yaml: service init is a one-shot but restart is not "no"'
    ]


def test_a_one_shot_never_needs_a_healthcheck(git_repo: Path) -> None:
    write_compose(
        git_repo,
        "services:\n"
        "  app:\n    healthcheck:\n      test: [CMD, true]\n"
        "    depends_on:\n      init:\n        condition: service_completed_successfully\n"
        '  init:\n    restart: "no"\n',
    )
    assert healthcheck_violations(git_repo) == []


# Structure


def test_flags_include(git_repo: Path) -> None:
    write_compose(git_repo, "include:\n  - other.yaml\nservices: {}\n")
    assert structure_violations(git_repo) == [
        "infra/compose.yaml: top-level include is not allowed (files are checked alone)"
    ]


def test_flags_extends(git_repo: Path) -> None:
    write_service(git_repo, "    extends:\n      file: base.yaml\n      service: base\n")
    assert structure_violations(git_repo) == ["infra/compose.yaml: service web uses extends"]


def test_a_plain_file_has_no_structure_violations(git_repo: Path) -> None:
    write_service(git_repo, GOOD_BODY)
    assert structure_violations(git_repo) == []


def test_the_env_rewrite_temp_file_is_git_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between mkstemp and os.replace must not leave an un-ignored copy of the secrets."""
    sources: list[Path] = []

    def failing_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        sources.append(Path(source))
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="replace failed"):
        stack.rewrite_env(tmp_path / stack.ENV_FILE.name, "A=1\n")
    assert len(sources) == 1
    temp = sources[0]
    assert temp.parent == tmp_path
    assert temp.name != stack.ENV_EXAMPLE.name
    assert list(tmp_path.iterdir()) == []
    assert _git(REPO, "check-ignore", "-q", f"infra/{temp.name}", check=False).returncode == 0


def test_a_compliant_service_passes_every_check(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text(
        "infra/.generated/\n.mem/\ninfra/.env\ninfra/.env.*\n", encoding="utf-8"
    )
    write_service(git_repo, GOOD_BODY + '    ports:\n      - "127.0.0.1:8181:8181"\n')
    for name, check in CHECKS.items():
        assert check(git_repo) == [], name


def test_violation_lists_are_sorted(git_repo: Path) -> None:
    write_compose(
        git_repo,
        "services:\n  b:\n    ports: [8, 9]\n  a:\n    ports: [7]\n    network_mode: host\n",
    )
    for name, check in CHECKS.items():
        result = check(git_repo)
        assert result == sorted(result), name


README_RUNTIME_STRINGS = (
    "Colima",
    "Apple Virtualization",
    "virtiofs",
    "12 GiB",
    "colima start --vm-type vz --memory 12 --cpu 4",
    "just up",
    "just down",
)


def test_readme_names_the_container_runtime() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    missing = [text for text in README_RUNTIME_STRINGS if text not in readme]
    assert missing == []


def test_agents_repo_map_lists_infra() -> None:
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "| `infra/`" in agents
