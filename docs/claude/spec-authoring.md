# Shopstream — Spec Authoring Rules

Enforced by the `/shop-spec` skill and `tests/test_docs_integrity.py`. Apply to every doc in `docs/specs/` and `docs/adr/`. The full style guide is [guide-doc-style.md](../specs/guide/guide-doc-style.md).

## Diagrams

- All diagrams are text-based (Mermaid or ASCII); no image attachments.
- Every diagram has a `> **Figure N**: <caption>` line above it.
- Sync flows use solid arrows; async flows use dashed arrows.
- Node labels use verbatim component names; edge labels state the protocol or payload.
- Mermaid blocks start with `%%{init: {'theme': 'neutral'}}%%`, which renders in light and dark mode.

## TRD structure

- `§1 Objective` · `§2 Architecture` · `§3 Data Model / Contract` · `§4 Risk Assessment` · `§5 Testing Strategy` · `§6 Operational Boundaries`.
- §2 has three sub-sections: Data Flow, Control Flow (what triggers what), Failure and Replay Flow (DLQ, retries, backfill, rebuild).
- §3 includes the patterns that apply: Topic and Schema Contract, Table Contract, Job Contract, Config Registry.
- §5 is a test pyramid, and names the test or `just` recipe that proves each claim.

## PRD structure

- Acceptance Criteria use EARS: `WHEN [trigger] THE SYSTEM SHALL [behavior]`, never plain bullets. Examples are in `docs/CONTRIBUTING.md`.

## ADRs

- Create them with `just adr-new "<title>" [owner]`. The number comes from the existing `docs/adr/adr-NNN-*.md` filenames, never from a counter.
- The body follows MADR 4.0 (`docs/tooling/adr-template.md`).

## Review gate

- Proposed → Accepted needs the `/shop-spec` Phase 6 critique at ≥ 13/15. Draft → Proposed can opt in at ≥ 11/15.
- The critique result goes in the PR description, never in the document.
- Use `/shop-spec` for new docs and `/shop-write-doc` for rewrites and reviews.
