"""Docs integrity lint: the conventions in docs/CONTRIBUTING.md, enforced.

Ported from docs-kit's docs-integrity.test.ts (vitest) to pytest. Each check takes a
repo root and returns violations, so the same function runs against the real tree and
against deliberately broken trees (the mutation tests below). A check that can't go red
proves nothing.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path("docs/tooling/frontmatter-schema.yaml")
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
        ".claude",
    }
)
REQUIRED_FIELDS = ("title", "type", "status", "owner")
RELATIONSHIP_KEYS = (
    "implements",
    "decided-by",
    "supersedes",
    "informs",
    "depends-on",
    "amends",
    "amended-by",
)
# Sections written during authoring that carry no durable information. Git owns the
# history; the typed frontmatter keys own the relationship graph.
RESIDUE_HEADINGS = ("critique score", "change log", "changelog", "relationship updates")
ADR_NAME = re.compile(r"adr-(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
SPEC_NAME = re.compile(r"(prd|trd|ref|guide|glossary)-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
FENCED_BLOCK = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=fence)[`~]*[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
MD_LINK = re.compile(r"\]\(([^)\s]+?\.mdx?)(?:#[^)]*)?\)")
ROOT_MARKDOWN = ("README.md", "AGENTS.md")

Check = Callable[[Path], list[str]]


@dataclass(frozen=True)
class Schema:
    types: frozenset[str]
    statuses: frozenset[str]
    owners: frozenset[str]


def load_schema(root: Path) -> Schema:
    raw = yaml.safe_load((root / SCHEMA_PATH).read_text(encoding="utf-8"))
    return Schema(
        types=frozenset(raw["type_enum"]),
        statuses=frozenset(raw["status_lifecycle"]),
        owners=frozenset(raw["owner_enum"]),
    )


def markdown_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.rglob("*.md*")
        if path.suffix in {".md", ".mdx"}
        and path.is_file()
        and not SKIP_DIRS.intersection(path.relative_to(directory).parts)
    )


def rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def frontmatter(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return (mapping, None); (None, None) when there is no block; (None, error) on bad YAML."""
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if match is None:
        return None, None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, f"invalid YAML frontmatter ({exc.__class__.__name__})"
    return (data if isinstance(data, dict) else {}), None


def body(path: Path) -> str:
    """The document text without its frontmatter and fenced code blocks."""
    text = FRONTMATTER.sub("", path.read_text(encoding="utf-8"), count=1)
    return FENCED_BLOCK.sub("", text)


def spec_and_adr_files(root: Path) -> list[Path]:
    return markdown_files(root / "docs" / "specs") + markdown_files(root / "docs" / "adr")


def check_adr_names(root: Path) -> list[str]:
    violations: list[str] = []
    by_number: dict[str, list[str]] = {}
    for path in markdown_files(root / "docs" / "adr"):
        match = ADR_NAME.fullmatch(path.name)
        if match is None:
            violations.append(f"{rel(root, path)}: name must match adr-NNN-<kebab-topic>.md")
            continue
        by_number.setdefault(match.group(1), []).append(path.name)
    for number, names in sorted(by_number.items()):
        if len(names) > 1:
            violations.append(f"docs/adr: ADR-{number} is used by {', '.join(sorted(names))}")
    return violations


def check_required_frontmatter(root: Path) -> list[str]:
    violations: list[str] = []
    for path in spec_and_adr_files(root):
        data, error = frontmatter(path)
        if error:
            violations.append(f"{rel(root, path)}: {error}")
        elif data is None:
            violations.append(f"{rel(root, path)}: no frontmatter")
        else:
            violations.extend(
                f"{rel(root, path)}: missing {key}"
                for key in REQUIRED_FIELDS
                if data.get(key) in (None, "")
            )
    return violations


def check_enum_values(root: Path) -> list[str]:
    schema = load_schema(root)
    legal = {"type": schema.types, "status": schema.statuses, "owner": schema.owners}
    violations: list[str] = []
    for path in spec_and_adr_files(root):
        data, _ = frontmatter(path)
        for key, allowed in legal.items():
            value = (data or {}).get(key)
            if value is not None and str(value) not in allowed:
                violations.append(
                    f"{rel(root, path)}: {key} {value!r} is not one of {sorted(allowed)}"
                )
    return violations


def check_relationships_resolve(root: Path) -> list[str]:
    violations: list[str] = []
    for path in markdown_files(root / "docs"):
        data, _ = frontmatter(path)
        for key in RELATIONSHIP_KEYS:
            if not data or key not in data:
                continue
            value = data[key]
            targets = [value] if isinstance(value, str) else value
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                violations.append(f"{rel(root, path)}: {key} must be a path or a list of paths")
                continue
            violations.extend(
                f"{rel(root, path)}: {key} -> {target} does not exist"
                for target in targets
                if not (path.parent / target).exists()
            )
    return violations


def check_residue_headings(root: Path) -> list[str]:
    violations: list[str] = []
    for path in markdown_files(root / "docs"):
        for match in HEADING.finditer(body(path)):
            heading = match.group(1).strip()
            normalized = re.sub(r"^[^a-z]+", "", re.sub(r"^[§\d.\s]+", "", heading.lower()))
            if normalized.startswith(RESIDUE_HEADINGS):
                violations.append(
                    f"{rel(root, path)}: residue heading {heading!r} (git is the history)"
                )
    return violations


def check_relative_links(root: Path) -> list[str]:
    violations: list[str] = []
    root_files = [root / name for name in ROOT_MARKDOWN if (root / name).is_file()]
    for path in markdown_files(root / "docs") + root_files:
        for match in MD_LINK.finditer(body(path)):
            href = match.group(1)
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / href).exists():
                violations.append(f"{rel(root, path)}: link -> {href} does not exist")
    return violations


def check_spec_filenames(root: Path) -> list[str]:
    violations: list[str] = []
    for path in markdown_files(root / "docs" / "specs"):
        if path.name == "README.md":
            continue
        match = SPEC_NAME.fullmatch(path.name)
        if match is None:
            violations.append(
                f"{rel(root, path)}: name must match <prd|trd|ref|guide|glossary>-<kebab>.md"
            )
            continue
        data, _ = frontmatter(path)
        doc_type = (data or {}).get("type")
        if doc_type is not None and doc_type != match.group(1):
            violations.append(
                f"{rel(root, path)}: filename prefix {match.group(1)}- but type {doc_type}"
            )
    return violations


CHECKS: dict[str, Check] = {
    "adr-names": check_adr_names,
    "required-frontmatter": check_required_frontmatter,
    "enum-values": check_enum_values,
    "relationships": check_relationships_resolve,
    "residue-headings": check_residue_headings,
    "relative-links": check_relative_links,
    "spec-filenames": check_spec_filenames,
}


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_repo_docs_pass(name: str) -> None:
    assert CHECKS[name](REPO) == []


# Mutation tests: every check must go red on a tree that breaks exactly its rule.

ADR = Path("docs/adr/adr-000-record-decisions.md")
REF = Path("docs/specs/platform/ref-thing.md")
VALID_ADR = """\
---
title: "ADR-000: Record decisions"
type: adr
status: Accepted
owner: platform
---

# ADR-000: Record decisions

See [the reference](../specs/platform/ref-thing.md#overview).
"""
VALID_REF = """\
---
title: Thing reference
type: ref
status: Draft  # a YAML comment is legal
owner: "analytics-eng"
informs: [../../adr/adr-000-record-decisions.md]
---

# Thing reference

````markdown
## Change Log
[an example link, not a real one](missing.md)
````

1. Check the build:

   ```bash
   echo "[not a link either](missing-too.md)"
   ```
"""


def build_tree(root: Path) -> None:
    (root / SCHEMA_PATH).parent.mkdir(parents=True)
    shutil.copy(REPO / SCHEMA_PATH, root / SCHEMA_PATH)
    for path, text in ((ADR, VALID_ADR), (REF, VALID_REF)):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def append(path: Path, extra: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")


def test_valid_tree_passes_every_check(tmp_path: Path) -> None:
    """Inline lists, quoted values, YAML comments and fenced examples are all legal."""
    build_tree(tmp_path)
    assert {name: check(tmp_path) for name, check in CHECKS.items()} == {
        name: [] for name in CHECKS
    }


Mutation = Callable[[Path], object]
MUTATIONS: list[tuple[str, Mutation, str]] = [
    (
        "adr-names",
        lambda r: shutil.copy(r / ADR, r / "docs/adr/adr-000-duplicate.md"),
        "ADR-000 is used by",
    ),
    ("adr-names", lambda r: (r / ADR).rename(r / "docs/adr/0001-record-decisions.md"), "adr-NNN"),
    ("required-frontmatter", lambda r: replace(r / ADR, "owner: platform\n", ""), "missing owner"),
    (
        "required-frontmatter",
        lambda r: (r / REF).write_text("# Thing\n", encoding="utf-8"),
        "no frontmatter",
    ),
    (
        "required-frontmatter",
        lambda r: replace(r / ADR, "type: adr\n", "type: [adr\n"),
        "invalid YAML",
    ),
    (
        "enum-values",
        lambda r: replace(r / ADR, "status: Accepted", "status: approved"),
        "status 'approved'",
    ),
    ("enum-values", lambda r: replace(r / ADR, "owner: platform", "owner: KMS"), "owner 'KMS'"),
    ("enum-values", lambda r: replace(r / REF, "type: ref", "type: spec"), "type 'spec'"),
    (
        "relationships",
        lambda r: replace(r / REF, "adr-000-record-decisions.md]", "adr-999-missing.md]"),
        "adr-999-missing.md does not exist",
    ),
    (
        "relationships",
        lambda r: replace(
            r / REF, "informs: [../../adr/adr-000-record-decisions.md]", "depends-on:"
        ),
        "must be a path or a list",
    ),
    ("residue-headings", lambda r: append(r / ADR, "\n## Change Log\n"), "'Change Log'"),
    (
        "residue-headings",
        lambda r: append(r / REF, "\n### 7. Critique Score\n"),
        "'7. Critique Score'",
    ),
    (
        "relative-links",
        lambda r: append(r / ADR, "\nSee [gone](gone.md#section).\n"),
        "gone.md does not exist",
    ),
    (
        "spec-filenames",
        lambda r: (r / REF).rename(r / "docs/specs/platform/thing.md"),
        "name must match",
    ),
    (
        "spec-filenames",
        lambda r: (r / REF).rename(r / "docs/specs/platform/guide-thing.md"),
        "prefix guide- but type ref",
    ),
    (
        "relative-links",
        lambda r: (r / "README.md").write_text("See [gone](docs/gone.md).\n", encoding="utf-8"),
        "docs/gone.md does not exist",
    ),
]


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    MUTATIONS,
    ids=[f"{name}-{index}" for index, (name, _, _) in enumerate(MUTATIONS)],
)
def test_check_goes_red_on_mutation(
    tmp_path: Path, name: str, mutate: Mutation, expected: str
) -> None:
    build_tree(tmp_path)
    mutate(tmp_path)
    violations = CHECKS[name](tmp_path)
    assert any(expected in violation for violation in violations), violations
