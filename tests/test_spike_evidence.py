"""The Week 2 spike evidence gate: 15 complete Item sections in docs/evidence/w2-spike.md.

Each check is a pure function over text, so the same function runs against the real file and
against deliberately broken texts (the mutation tests below). A check that can't go red proves
nothing. The tests read the evidence; they never change it, and they pin no ADR status value.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "docs/evidence/w2-spike.md"
ADR = REPO / "docs/adr/adr-001-feasibility-spike.md"

ITEM_COUNT = 15
# The Item contract says about 40 lines; Item 8's 47-line per-service table is the longest.
MAX_FENCE_LINES = 50
# ADR-001's Evidence rules: one-off load scripts for items 10 to 12 record their command line
# and seed, so the section can be rerun.
SEEDED_ITEMS = (10, 11, 12)

FENCED_BLOCK = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^[ \t]*(?P=fence)[`~]*[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
ITEM_HEADING = re.compile(r"^## Item (\d+):", re.MULTILINE)
LEVEL_2_HEADING = re.compile(r"^## ", re.MULTILINE)
RECORDED_LINE = re.compile(r"^Recorded (from )?\d{4}-\d{2}-\d{2}", re.MULTILINE)
VERSIONS_HEADING = re.compile(r"^### Versions", re.MULTILINE)
VERDICT_LINE = re.compile(r"^Verdict: .*$", re.MULTILINE)
COMMAND_MARKERS = ("$ ", "docker compose ", "uv run ", "python ")
VALID_VERDICT_STARTS = ("Verdict: go. ", "Verdict: fallback. ")


def mask_fences(text: str) -> str:
    """Blank every fenced block's characters but not its newlines: line numbers stay, and a
    heading-looking line inside a fence is not seen."""

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return FENCED_BLOCK.sub(blank, text)


def item_headings(text: str) -> list[int]:
    """The N of every `## Item N:` line outside a fence, in file order."""
    return [int(n) for n in ITEM_HEADING.findall(mask_fences(text))]


def item_sections(text: str) -> dict[int, str]:
    """Each Item's first section: from its heading to the next level-2 heading or the end.

    A `###` subsection stays in its Item, and so does a `## ` line inside a fence.
    """
    masked_lines = mask_fences(text).split("\n")
    lines = text.split("\n")
    sections: dict[int, str] = {}
    start: int | None = None
    number = 0
    for index, line in enumerate([*masked_lines, "## end"]):
        if start is not None and LEVEL_2_HEADING.match(line):
            sections.setdefault(number, "\n".join(lines[start:index]))
            start = None
        heading = ITEM_HEADING.match(line)
        if heading is not None:
            start = index
            number = int(heading.group(1))
    return sections


def fenced_bodies(section: str) -> list[list[str]]:
    """The lines between the fences of each fenced block in a section."""
    return [match.group(0).split("\n")[1:-1] for match in FENCED_BLOCK.finditer(section)]


def section_violations(number: int, section: str) -> list[str]:
    masked = mask_fences(section)
    found: list[str] = []
    if RECORDED_LINE.search(masked) is None:
        found.append(f"Item {number}: no Recorded date")
    if VERSIONS_HEADING.search(masked) is None:
        found.append(f"Item {number}: no ### Versions subsection")
    bodies = fenced_bodies(section)
    if not bodies:
        found.append(f"Item {number}: no fenced block")
    if not any(marker in line for body in bodies for line in body for marker in COMMAND_MARKERS):
        found.append(f"Item {number}: no command in a fenced block")
    verdicts = VERDICT_LINE.findall(masked)
    if len(verdicts) != 1:
        found.append(f"Item {number}: {len(verdicts)} Verdict lines")
    if verdicts and not verdicts[0].startswith(VALID_VERDICT_STARTS):
        found.append(f"Item {number}: verdict must be go or fallback")
    found.extend(
        f"Item {number}: fenced block of {len(body)} lines (limit {MAX_FENCE_LINES})"
        for body in bodies
        if len(body) > MAX_FENCE_LINES
    )
    if number in SEEDED_ITEMS and re.search("seed", section, re.IGNORECASE) is None:
        found.append(f"Item {number}: no seed recorded")
    return found


def evidence_violations(text: str) -> list[str]:
    """Every way the evidence text falls short of 15 complete, ordered Item sections."""
    headings = item_headings(text)
    sections = item_sections(text)
    found: list[str] = []
    for number in range(1, ITEM_COUNT + 1):
        count = headings.count(number)
        if count == 0:
            found.append(f"Item {number}: missing")
        elif count > 1:
            found.append(f"Item {number}: appears {count} times")
    found.extend(
        f"Item {number}: unexpected Item number"
        for number in sorted(set(headings))
        if not 1 <= number <= ITEM_COUNT
    )
    if headings != sorted(headings):
        found.append("Item headings out of order")
    for number in sorted(sections):
        found.extend(section_violations(number, sections[number]))
    return found


def synthetic_item(number: int) -> str:
    seed = "\nThe generator's seed is 42.\n" if number in SEEDED_ITEMS else ""
    return (
        f"## Item {number}: Synthetic item {number}\n\n"
        "Recorded 2026-10-01.\n\n"
        "### Versions\n\n"
        "- Tool 1.0.0.\n\n"
        "### Commands\n\n"
        "```text\n"
        f"$ uv run synthetic --item {number}\n"
        "ok\n"
        "```\n"
        f"{seed}\n"
        "Verdict: go. The synthetic check passed.\n\n"
    )


def valid_evidence() -> str:
    """A synthetic evidence text: a preamble, a non-Item section and 15 complete Items."""
    preamble = (
        "# Synthetic evidence\n\nRecorded from 2026-10-01.\n\n## Platform base\n\nNot an Item.\n\n"
    )
    return preamble + "".join(synthetic_item(number) for number in range(1, ITEM_COUNT + 1))


def edit_item(text: str, number: int, edit: Callable[[str], str]) -> str:
    section = item_sections(text)[number]
    return text.replace(section, edit(section), 1)


def with_fence_of(lines: int) -> Callable[[str], str]:
    """Replace an Item's fenced block with one of `lines` lines, the first a `$ ` command."""
    body = "$ uv run synthetic\n" + "filler\n" * (lines - 1)
    return lambda section: FENCED_BLOCK.sub(lambda _: f"```text\n{body}```", section, count=1)


def swap_items(text: str, first: int, second: int) -> str:
    sections = item_sections(text)
    return (
        text.replace(sections[first], "@@FIRST@@", 1)
        .replace(sections[second], "@@SECOND@@", 1)
        .replace("@@FIRST@@", sections[second], 1)
        .replace("@@SECOND@@", sections[first], 1)
    )


Mutation = Callable[[str], str]

EVIDENCE_MUTATIONS: list[tuple[str, Mutation, str]] = [
    (
        "item-4-no-versions",
        lambda t: edit_item(t, 4, lambda s: s.replace("### Versions", "### Pins")),
        "Item 4: no ### Versions",
    ),
    (
        "item-13-no-versions",
        lambda t: edit_item(t, 13, lambda s: s.replace("### Versions\n\n- Tool 1.0.0.\n\n", "")),
        "Item 13: no ### Versions",
    ),
    (
        "item-6-no-recorded-date",
        lambda t: edit_item(t, 6, lambda s: s.replace("Recorded 2026-10-01.\n\n", "")),
        "Item 6: no Recorded date",
    ),
    (
        "item-4-malformed-recorded-date",
        lambda t: edit_item(
            t, 4, lambda s: s.replace("Recorded 2026-10-01.", "Recorded yesterday.")
        ),
        "Item 4: no Recorded date",
    ),
    (
        "item-9-verdict-maybe",
        lambda t: edit_item(t, 9, lambda s: s.replace("Verdict: go. ", "Verdict: maybe. ")),
        "Item 9: verdict must be go or fallback",
    ),
    (
        "item-9-verdict-without-the-sentence-gap",
        lambda t: edit_item(t, 9, lambda s: s.replace("Verdict: go. ", "Verdict: go")),
        "Item 9: verdict must be go or fallback",
    ),
    (
        "item-2-second-verdict",
        lambda t: edit_item(t, 2, lambda s: s + "Verdict: go. Again.\n\n"),
        "Item 2: 2 Verdict lines",
    ),
    (
        "item-3-no-verdict",
        lambda t: edit_item(
            t, 3, lambda s: s.replace("Verdict: go. The synthetic check passed.\n", "")
        ),
        "Item 3: 0 Verdict lines",
    ),
    (
        "item-3-no-fence",
        lambda t: edit_item(t, 3, lambda s: FENCED_BLOCK.sub("", s)),
        "Item 3: no fenced block",
    ),
    (
        "item-7-no-command",
        lambda t: edit_item(
            t, 7, lambda s: s.replace("$ uv run synthetic --item 7", "output only")
        ),
        "Item 7: no command in a fenced block",
    ),
    (
        "item-7-fence-of-51-lines",
        lambda t: edit_item(t, 7, with_fence_of(MAX_FENCE_LINES + 1)),
        "Item 7: fenced block of 51 lines",
    ),
    (
        "item-10-no-seed",
        lambda t: edit_item(t, 10, lambda s: s.replace("seed", "number")),
        "Item 10: no seed",
    ),
    (
        "item-11-no-seed",
        lambda t: edit_item(t, 11, lambda s: s.replace("seed", "number")),
        "Item 11: no seed",
    ),
    (
        "item-12-no-seed",
        lambda t: edit_item(t, 12, lambda s: s.replace("seed", "number")),
        "Item 12: no seed",
    ),
    (
        "item-15-missing",
        lambda t: edit_item(t, 15, lambda _: ""),
        "Item 15: missing",
    ),
    (
        "item-5-heading-duplicated",
        lambda t: edit_item(t, 5, lambda s: s + s),
        "Item 5: appears 2 times",
    ),
    (
        "items-1-and-2-swapped",
        lambda t: swap_items(t, 1, 2),
        "out of order",
    ),
    (
        "item-16-added",
        lambda t: t + "## Item 16: Extra\n\nRecorded 2026-10-01.\n",
        "Item 16: unexpected Item number",
    ),
    ("empty-text-item-1", lambda _: "", "Item 1: missing"),
    ("empty-text-item-15", lambda _: "", "Item 15: missing"),
]


def test_the_evidence_file_is_complete() -> None:
    text = EVIDENCE.read_text(encoding="utf-8")
    assert evidence_violations(text) == []


def test_a_valid_synthetic_evidence_text_passes() -> None:
    assert evidence_violations(valid_evidence()) == []


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    EVIDENCE_MUTATIONS,
    ids=[name for name, _, _ in EVIDENCE_MUTATIONS],
)
def test_evidence_check_goes_red_on_mutation(name: str, mutate: Mutation, expected: str) -> None:
    violations = evidence_violations(mutate(valid_evidence()))
    assert any(expected in violation for violation in violations), (name, violations)


def test_an_empty_text_reports_all_15_items_missing() -> None:
    assert evidence_violations("") == [f"Item {n}: missing" for n in range(1, ITEM_COUNT + 1)]


def test_an_item_heading_with_nothing_under_it_names_each_missing_part() -> None:
    text = edit_item(valid_evidence(), 12, lambda _: "## Item 12: Bare\n\n")
    violations = [v for v in evidence_violations(text) if v.startswith("Item 12: ")]
    expected = (
        "no Recorded date",
        "no ### Versions subsection",
        "no fenced block",
        "no command in a fenced block",
        "0 Verdict lines",
        "no seed recorded",
    )
    assert violations == [f"Item 12: {part}" for part in expected]


def test_a_fence_of_exactly_50_lines_passes() -> None:
    text = edit_item(valid_evidence(), 7, with_fence_of(MAX_FENCE_LINES))
    assert evidence_violations(text) == []


def test_a_level_2_line_inside_a_fence_stays_in_its_item() -> None:
    text = edit_item(
        valid_evidence(), 8, lambda s: s.replace("ok\n", "## Item 99: inside a fence\n")
    )
    assert item_headings(text) == list(range(1, ITEM_COUNT + 1))
    assert "## Item 99: inside a fence" in item_sections(text)[8]
    assert evidence_violations(text) == []


def test_a_level_3_subsection_stays_in_its_item() -> None:
    section = item_sections(valid_evidence())[3]
    assert "### Versions" in section
    assert "### Commands" in section
    assert "## Item 4:" not in section
