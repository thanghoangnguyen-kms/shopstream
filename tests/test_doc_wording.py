"""Wording rules carried over from Week 1 and the spec-first loop, pinned.

The authoring skill and the contributing guide state rules that other files repeat: the
critique's CDC questions, how Phase 0 treats an outside document, which GUIDE sections
apply and when, and which ADR sections freeze. This module also bans the em and en dash
in the authoring files, which style-and-tone.md forbids. It reads files only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".claude/skills/shop-spec"
# Written with escapes so this file holds no dash of its own.
DASH = re.compile(r"[\u2013\u2014]")
DASH_FREE: tuple[Path, ...] = (
    SKILL / "SKILL.md",
    SKILL / "references/conventions.md",
    SKILL / "references/critique-protocol.md",
    SKILL / "references/templates.md",
)

FROZEN_RULE = "Context, Decision Outcome or Consequences after `status: Accepted`"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def between(text: str, start: str, end: str) -> str:
    """The text after the first `start` and before the next `end` (to the end if none)."""
    _, found, rest = text.partition(start)
    assert found, f"{start!r} not found"
    return rest.partition(end)[0]


def line_starting(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starts with {prefix!r}")


@pytest.mark.parametrize("path", DASH_FREE, ids=lambda path: path.relative_to(REPO).as_posix())
def test_no_em_or_en_dashes(path: Path) -> None:
    lines = [n for n, line in enumerate(read(path).splitlines(), 1) if DASH.search(line)]
    assert not lines, f"em or en dash on lines {lines}"


def test_critique_protocol_orders_cdc_by_lsn_per_key() -> None:
    text = read(SKILL / "references/critique-protocol.md")
    critic = between(text, "### Data Correctness Critic", "\n### ")
    assert "LSN per key; never a timestamp" in line_starting(critic, "2. ")
    third = line_starting(critic, "3. ")
    assert "SCD2 validity from the simulated `updated_at` (the business clock)" in third


def test_phase_0_treats_outside_input_as_input_only() -> None:
    phase_0 = between(read(SKILL / "SKILL.md"), "### Phase 0", "### Phase 1")
    assert "input only" in phase_0
    assert "never copy its paths, links" in phase_0


def test_guide_sections_apply_to_procedural_guides_and_runbooks() -> None:
    skill = read(SKILL / "SKILL.md")
    templates = read(SKILL / "references/templates.md")
    assert "procedural guides and runbooks" in line_starting(skill, "- **GUIDE**")
    assert "procedural guides and runbooks" in line_starting(templates, "### GUIDE Scaffold")


def test_accepted_adrs_freeze_only_three_sections() -> None:
    for path in (SKILL / "SKILL.md", SKILL / "references/conventions.md"):
        text = read(path)
        assert FROZEN_RULE in text, path.name
        assert "immutable" not in text.lower(), path.name
