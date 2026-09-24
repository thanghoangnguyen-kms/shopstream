"""Adapted docs-kit skills: loadable, registered in AGENTS.md, and free of the kit's defects.

The kit's templates wrote headings that the docs lint bans (Change Log, Critique Score,
Relationship Updates) and pointed at artifacts of the source repo. These checks keep
both classes of defect from coming back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / ".claude" / "skills"
EXPECTED = {"shop-spec", "shop-write-doc", "shop-housekeeping"}
BANNED_HEADING = re.compile(
    r"^\s*#{1,6}\s+(change ?log|critique score|relationship updates)", re.IGNORECASE | re.MULTILINE
)
DEAD_ARTIFACTS = re.compile(r"INDEX\.md|TRACEABILITY\.md|retype|codegraph", re.IGNORECASE)
SOURCE_RESIDUE = re.compile(r"\b(icc|enstructure)\b", re.IGNORECASE)
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def skill_dirs() -> list[Path]:
    return sorted(path for path in SKILLS.iterdir() if path.is_dir()) if SKILLS.is_dir() else []


def skill_meta(directory: Path) -> dict[str, str]:
    match = FRONTMATTER.match((directory / "SKILL.md").read_text(encoding="utf-8"))
    assert match, f"{directory.name}/SKILL.md has no frontmatter"
    meta: dict[str, str] = yaml.safe_load(match.group(1))
    return meta


def skill_markdown() -> list[Path]:
    return sorted(SKILLS.rglob("*.md")) if SKILLS.is_dir() else []


def test_expected_skills_are_installed() -> None:
    assert {path.name for path in skill_dirs()} == EXPECTED


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda path: path.name)
def test_skill_name_matches_directory(directory: Path) -> None:
    assert skill_meta(directory)["name"] == directory.name


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda path: path.name)
def test_skill_description_is_shopstream_specific(directory: Path) -> None:
    description = skill_meta(directory)["description"]
    assert "Shopstream" in description
    assert not SOURCE_RESIDUE.search(description)


def test_agents_md_lists_every_skill() -> None:
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    section = agents.split("## 5. Skills", 1)[1].split("\n## ", 1)[0]
    assert set(re.findall(r"`/(shop-[a-z-]+)`", section)) == EXPECTED


@pytest.mark.parametrize(
    "path", skill_markdown(), ids=lambda path: path.relative_to(SKILLS).as_posix()
)
def test_skill_files_carry_no_kit_defects(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not BANNED_HEADING.search(text), "writes a heading the docs lint bans"
    assert not DEAD_ARTIFACTS.search(text), "points at an artifact that doesn't exist here"
    assert not SOURCE_RESIDUE.search(text), "still names the source repo"
