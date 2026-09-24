"""Create the next ADR (docs/adr/adr-NNN-<slug>.md) from docs/tooling/adr-template.md.

Numbers come from the filenames already in docs/adr/, never from a counter, so two
ADRs can't share a number (tests/test_docs_integrity.py also enforces this).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = REPO_ROOT / "docs" / "adr"
TEMPLATE = REPO_ROOT / "docs" / "tooling" / "adr-template.md"
ADR_FILE = re.compile(r"adr-(\d{3})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md")
MAX_NUMBER = 999


def slugify(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    if not slug:
        raise ValueError(f"title {title!r} has no letters or digits to build a slug from")
    return slug


def existing_adrs(adr_dir: Path) -> dict[str, int]:
    """Map each existing ADR slug to its number."""
    found: dict[str, int] = {}
    if adr_dir.is_dir():
        for path in adr_dir.glob("adr-*.md"):
            match = ADR_FILE.fullmatch(path.name)
            if match:
                found[match.group(2)] = int(match.group(1))
    return found


def next_number(adr_dir: Path) -> int:
    number = max(existing_adrs(adr_dir).values(), default=-1) + 1
    if number > MAX_NUMBER:
        raise ValueError(f"ADR numbers are 3 digits; adr-{MAX_NUMBER:03d} is the last one")
    return number


def render(template: str, number: int, title: str, owner: str, today: dt.date) -> str:
    if '"' in title:
        raise ValueError(
            "ADR titles can't contain double quotes (they sit in a quoted YAML string)"
        )
    values = {
        "{{number}}": f"{number:03d}",
        "{{title}}": title,
        "{{owner}}": owner,
        "{{date}}": today.isoformat(),
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return template


def create(title: str, adr_dir: Path, template_path: Path, owner: str, today: dt.date) -> Path:
    slug = slugify(title)
    existing = existing_adrs(adr_dir)
    if slug in existing:
        raise FileExistsError(
            f"ADR topic {slug!r} already exists as adr-{existing[slug]:03d}-{slug}.md"
        )
    number = next_number(adr_dir)
    target = adr_dir / f"adr-{number:03d}-{slug}.md"
    adr_dir.mkdir(parents=True, exist_ok=True)
    rendered = render(template_path.read_text(encoding="utf-8"), number, title, owner, today)
    target.write_text(rendered, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "title", help='ADR title, e.g. "Use Lakekeeper as the Iceberg REST catalog"'
    )
    parser.add_argument("--owner", default="platform", help="platform | analytics-eng | governance")
    args = parser.parse_args(argv)
    path = create(args.title, ADR_DIR, TEMPLATE, args.owner, dt.date.today())
    print(path.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
