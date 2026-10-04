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
import yaml

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".claude/skills/shop-spec"
CONTRIBUTING = REPO / "docs/CONTRIBUTING.md"
SCHEMA = REPO / "docs/tooling/frontmatter-schema.yaml"
PRE_COMMIT = REPO / ".pre-commit-config.yaml"
# Written with escapes so this file holds no dash of its own.
DASH = re.compile(r"[\u2013\u2014]")
DASH_FREE: tuple[Path, ...] = (
    SKILL / "SKILL.md",
    SKILL / "references/conventions.md",
    SKILL / "references/critique-protocol.md",
    SKILL / "references/templates.md",
    CONTRIBUTING,
    SCHEMA,
)

FROZEN_RULE = "Context, Decision Outcome or Consequences after `status: Accepted`"
PIN_PHRASES = (
    "Four pins live outside uv.lock",
    "gitleaks",
    "GitHub Actions SHAs",
    "pre-commit-hooks rev",
    "uv itself",
    "prek runs its own built-in code",
)


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


def leading_comment(text: str) -> str:
    """The run of `#` lines at the top of a file, up to the first line that isn't one."""
    comment: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        comment.append(line)
    return "\n".join(comment)


def missing_pin_phrases(comment: str) -> list[str]:
    """The pin phrases the comment omits; matching is case-sensitive."""
    return [phrase for phrase in PIN_PHRASES if phrase not in comment]


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
    guide = between(read(CONTRIBUTING), "### GUIDE", "\n### ")
    assert "Procedural guides and runbooks" in guide


def test_accepted_adrs_freeze_only_three_sections() -> None:
    for path in (SKILL / "SKILL.md", SKILL / "references/conventions.md"):
        text = read(path)
        assert FROZEN_RULE in text, path.name
        assert "immutable" not in text.lower(), path.name
    contributing = read(CONTRIBUTING)
    adr = between(contributing, "### ADR (MADR 4.0 body)", "\n### ")
    assert "only Context, Decision Outcome and Consequences are frozen" in adr
    assert FROZEN_RULE in between(contributing, "## Boundaries", "\n## ")
    schema = read(SCHEMA)
    adr_type: str = yaml.safe_load(schema)["type_enum"]["adr"]
    assert "Context, Decision Outcome and Consequences" in adr_type
    for text in (contributing, schema):
        assert "immutable" not in text.lower()


def test_pre_commit_comment_names_the_four_pins() -> None:
    comment = leading_comment(read(PRE_COMMIT))
    assert missing_pin_phrases(comment) == []


GOOD_COMMENT = "\n".join(f"# {phrase}" for phrase in PIN_PHRASES)


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        (GOOD_COMMENT, []),
        (GOOD_COMMENT.replace("uv itself", "uv"), ["uv itself"]),
        (
            GOOD_COMMENT.replace("GitHub Actions SHAs", "github actions shas"),
            ["GitHub Actions SHAs"],
        ),
        ("default_install_hook_types: [pre-commit]\n" + GOOD_COMMENT, list(PIN_PHRASES)),
    ],
    ids=["complete", "omits-uv", "wrong-case", "below-the-comment-block"],
)
def test_pin_helper_reports_what_the_comment_omits(text: str, missing: list[str]) -> None:
    assert missing_pin_phrases(leading_comment(text)) == missing
