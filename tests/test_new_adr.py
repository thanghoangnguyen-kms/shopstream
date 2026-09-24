"""new_adr: numbering, slugs and rendering behind `just adr-new`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import new_adr
import pytest

TEMPLATE = (
    '---\ntitle: "ADR-{{number}}: {{title}}"\nowner: {{owner}}\ncreated: {{date}}\n---\n\n'
    "# ADR-{{number}}: {{title}}\n"
)
TODAY = dt.date(2026, 9, 24)


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
