"""new_adr: numbering, slugs and rendering behind `just adr-new`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import new_adr
import pytest
import yaml

TEMPLATE = (
    '---\ntitle: "ADR-{{number}}: {{title}}"\nowner: {{owner}}\ncreated: {{date}}\n---\n\n'
    "# ADR-{{number}}: {{title}}\n"
)
TODAY = dt.date(2026, 9, 24)
REAL_TEMPLATE = Path(__file__).resolve().parents[1] / "docs" / "tooling" / "adr-template.md"


def frontmatter_of(rendered: str) -> dict[str, Any]:
    """Load the YAML between the first two `---` lines of a rendered ADR."""
    _, block, _ = rendered.split("---\n", 2)
    loaded = yaml.safe_load(block)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Record architecture decisions", "record-architecture-decisions"),
        ("Use Iceberg REST catalog (Lakekeeper)", "use-iceberg-rest-catalog-lakekeeper"),
        ("Écrire  un café — vite", "ecrire-un-cafe-vite"),
        ("dbt 2.0 vs dbt-core 1.12", "dbt-2-0-vs-dbt-core-1-12"),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert new_adr.slugify(title) == slug


def test_slugify_rejects_title_without_letters_or_digits() -> None:
    with pytest.raises(ValueError, match="no letters or digits"):
        new_adr.slugify("!!! —")


def test_next_number_starts_at_zero(tmp_path: Path) -> None:
    assert new_adr.next_number(tmp_path) == 0


def test_next_number_is_highest_plus_one(tmp_path: Path) -> None:
    for name in ("adr-000-a.md", "adr-002-b.md", "adr-template.md", "notes.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert new_adr.next_number(tmp_path) == 3


def test_next_number_refuses_past_999(tmp_path: Path) -> None:
    (tmp_path / "adr-999-last.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="999"):
        new_adr.next_number(tmp_path)


def test_render_fills_every_placeholder() -> None:
    rendered = new_adr.render(TEMPLATE, 7, "Use X", "platform", TODAY)
    assert 'title: "ADR-007: Use X"' in rendered
    assert "owner: platform" in rendered
    assert "created: 2026-09-24" in rendered
    assert "{{" not in rendered


def test_render_rejects_double_quotes() -> None:
    with pytest.raises(ValueError, match="double quotes"):
        new_adr.render(TEMPLATE, 1, 'Use "X"', "platform", TODAY)


def test_create_writes_the_next_adr(tmp_path: Path) -> None:
    template = tmp_path / "adr-template.md"
    template.write_text(TEMPLATE, encoding="utf-8")
    adr_dir = tmp_path / "adr"
    path = new_adr.create("Record architecture decisions", adr_dir, template, "platform", TODAY)
    assert path == adr_dir / "adr-000-record-architecture-decisions.md"
    assert path.read_text(encoding="utf-8").startswith('---\ntitle: "ADR-000: Record')


def test_create_refuses_duplicate_topic(tmp_path: Path) -> None:
    template = tmp_path / "adr-template.md"
    template.write_text(TEMPLATE, encoding="utf-8")
    adr_dir = tmp_path / "adr"
    new_adr.create("Pick a catalog", adr_dir, template, "platform", TODAY)
    with pytest.raises(FileExistsError, match="pick-a-catalog"):
        new_adr.create("Pick a catalog!", adr_dir, template, "platform", TODAY)


@pytest.mark.parametrize(
    "title",
    [
        "Use Lakekeeper: the REST catalog",
        "C# or F#, pick one",
        "Cafe & naive letters: \u00e9, \u00fc",
        "it's 100% done",
        "Paths a/b and #anchors",
    ],
)
def test_render_with_the_real_template_parses_as_yaml(title: str) -> None:
    template = REAL_TEMPLATE.read_text(encoding="utf-8")
    rendered = new_adr.render(template, 7, title, "platform", TODAY)
    meta = frontmatter_of(rendered)
    assert meta["title"] == "ADR-007: " + title
    assert meta["type"] == "adr"
    assert meta["status"] == "Draft"
    assert meta["owner"] == "platform"
    assert meta["created"] == TODAY
    assert "{{" not in rendered


@pytest.mark.parametrize("title", ["C:" + chr(92) + "temp", "line" + chr(92) + "n break"])
def test_render_rejects_backslashes(title: str) -> None:
    with pytest.raises(ValueError, match="backslash"):
        new_adr.render(TEMPLATE, 1, title, "platform", TODAY)


@pytest.mark.parametrize("char", [chr(9), chr(10), chr(0)])
def test_render_rejects_control_characters(char: str) -> None:
    with pytest.raises(ValueError, match="control character"):
        new_adr.render(TEMPLATE, 1, "Use" + char + "X", "platform", TODAY)


@pytest.mark.parametrize("title", ["Use {{owner}}", "a }} b"])
def test_render_rejects_template_braces(title: str) -> None:
    with pytest.raises(ValueError, match="template brace"):
        new_adr.render(TEMPLATE, 1, title, "platform", TODAY)


def test_create_with_the_real_template(tmp_path: Path) -> None:
    adr_dir = tmp_path / "adr"
    path = new_adr.create("Use X: a decision", adr_dir, REAL_TEMPLATE, "governance", TODAY)
    assert path == adr_dir / "adr-000-use-x-a-decision.md"
    assert frontmatter_of(path.read_text(encoding="utf-8"))["owner"] == "governance"
