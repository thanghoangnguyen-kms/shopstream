---
name: shop-spec
description: Guides authoring of any Shopstream design document (PRD, TRD, ADR, REF, GUIDE, GLOSSARY) under docs/specs/ or docs/adr/. Enforces docs/CONTRIBUTING.md (frontmatter schema, owner roles platform/analytics-eng/governance, required sections, EARS acceptance criteria, MADR ADR bodies), audits the doc against the Platform gotchas in AGENTS.md, runs an optional three-critic review gate (at least 13/15 to become Accepted), and finishes with a compression pass and a relationship sweep. Covers data-pipeline TRDs (topics, tables, jobs, config). Use when creating or substantially revising any doc in docs/specs/ or docs/adr/.
---

# Shopstream Spec Authoring Skill

Reference material: [references/templates.md](references/templates.md) · [references/conventions.md](references/conventions.md) · [references/critique-protocol.md](references/critique-protocol.md)

## Roles

| Role        | Phase | Responsibility                                                              |
| ----------- | ----- | --------------------------------------------------------------------------- |
| interviewer | 0–1   | Scan `docs/adr/` and `docs/specs/`; elicit type, domain, slug, owner, links |
| planner     | 2–3   | Generate frontmatter and the section skeleton                               |
| writer      | 4     | Produce the full document                                                   |
| auditor     | 5     | Run the Platform-gotcha audit and the docs lint                             |
| critic      | 6     | Run the three Shopstream critics; score the 5-dimension rubric              |
| compressor  | 7     | Run the compression checklist; list and apply relationship patches          |

## Phases

### Phase 0 — context_scan

- Derive the next ADR number from `docs/adr/` filenames (`ls docs/adr | sort | tail -1`), or let `just adr-new` assign it. Never use a remembered number.
- Glob `docs/specs/` and `docs/adr/` to confirm the proposed slug is free.
- Read `AGENTS.md` §6 (Platform gotchas) and `docs/specs/platform/ref-architecture.md` before writing any architecture claim.

### Phase 1 — doc_type_interview

Ask what is being documented, and map it to a type:

| Intent                                        | Type       | Path                                                  |
| --------------------------------------------- | ---------- | ----------------------------------------------------- |
| WHAT a data product delivers to its consumers | `prd`      | `docs/specs/<domain>/prd-<kebab>.md`                  |
| HOW a pipeline, job or service is built       | `trd`      | `docs/specs/<domain>/trd-<kebab>.md`                  |
| Point-in-time binding decision                | `adr`      | `docs/adr/adr-NNN-<topic>.md` via `just adr-new`      |
| Normative data/topology SSOT                  | `ref`      | `docs/specs/ref/ref-<kebab>.md`, or its domain folder |
| Procedural how-to or runbook                  | `guide`    | `docs/specs/<domain>/guide-<kebab>.md` (runbooks: `guide-runbook-<failure>.md`) |
| Term definitions                              | `glossary` | `docs/specs/ref/glossary-<kebab>.md`                  |

Domains: `platform`, `ingestion`, `transform`, `streaming`, `orchestration`, `ai`. Create the folder with its first doc.

Also collect the owner (`platform` | `analytics-eng` | `governance`) and the paths for the typed relationship keys.

### Phase 2 — frontmatter_generation

Use the templates in [references/templates.md](references/templates.md):

- Required: `title`, `type`, `status: Draft`, `owner`
- `version:` for PRD / TRD / REF only (semver, starting at `1.0.0`)
- `decision:` for ADRs only (one line); no `version:`
- `created:` / `updated:` set to today's date (ISO 8601)
- Typed relationships (`implements`, `decided-by`, `depends-on`, `informs`, `supersedes`, `amends`, `amended-by`) take relative paths

### Phase 3 — writing_plan

Present the required sections for the chosen type (from `docs/CONTRIBUTING.md`). Ask for content inputs per section before writing.

### Phase 4 — writing

Produce the complete document from the scaffold in [references/templates.md](references/templates.md):

- **PRD:** Objective and Consumers → Functional Requirements → NFR table → Acceptance Criteria (EARS) → Out of Scope → Open Questions
- **TRD:** `§1 Objective` → `§2 Architecture` (Data Flow / Control Flow / Failure and Replay Flow) → `§3 Data Model / Contract` (Topic and Schema / Table / Job / Config) → `§4 Risk Assessment` → `§5 Testing Strategy` → `§6 Operational Boundaries`
- **ADR:** `just adr-new "<title>" [owner]`, then fill the MADR 4.0 body: Context and Problem Statement → Decision Drivers → Considered Options → Decision Outcome (Consequences, Confirmation) → Pros and Cons of the Options → More Information
- **REF:** Overview (SSOT declaration) → Core definitions → Precedence Rules
- **GUIDE:** Overview + Prerequisites → Procedure → Troubleshooting → Operational Boundaries
- **GLOSSARY:** one Term / Definition / Defined by table

EARS format, for PRD Acceptance Criteria:

```
WHEN [trigger] THE SYSTEM SHALL [observable behavior]
IF [precondition] THEN THE SYSTEM SHALL [behavior]
THE SYSTEM SHALL [always-on requirement]
```

Git is the history. Never add a change log, critique score or relationship-updates section to a document; the docs lint fails the build on those headings.

### Phase 5 — gotcha_audit

Run [references/conventions.md](references/conventions.md) §Platform-gotcha audit against the produced doc. Flag each contradiction as `G<N>: <claim> → <correct statement>` and fix it. Then run:

```bash
uv run pytest tests/test_docs_integrity.py -q
```

The document isn't done until that passes.

### Phase 6 — critique

- Trigger: **mandatory** for Proposed → Accepted; opt-in for Draft → Proposed.
- Protocol: [references/critique-protocol.md](references/critique-protocol.md).
- Process: run the Data Correctness, Reliability & Operability, and Security & Privacy critics as **independent subagents**, dispatched in parallel.
- Score: the 5-dimension rubric (Completeness · Implementability · Testability · Risk Coverage · Owner Attribution).
- Gate: ≥ 11 / 15 to advance to Proposed · ≥ 13 / 15 to advance to Accepted.
- Output: the score table and the unresolved flags go in the **PR description** (or the chat), never in the document. The writer resolves each flag, or logs it with an owner role and a target week: in Open Questions for a PRD, in the PR description for every other type.

### Phase 7 — spec_compression_and_relationship_sweep

Trigger: always, after Phase 4 (or after Phase 6 when a critique ran), before the final commit.

**Step 7a — Compression.** Run the checklist in [references/templates.md](references/templates.md) §Phase 7 and fix each violation inline.

**Step 7b — Relationship sweep.** Follow [references/conventions.md](references/conventions.md) §Relationship Sweep Protocol:

1. Grep `docs/` for the new doc's primary components, tables, topics and ADRs.
2. Classify each impacted doc's relationship.
3. Put the patch list in the PR description or the chat, and **apply the patches in the same PR**. Never add the list to the document.

## Constraints

- New docs always start at `status: Draft`; never at `Accepted` or `Active`.
- ADR bodies are immutable after `status: Accepted`; supersede them with a new ADR.
- Every deliverable has exactly one owner role (see [references/conventions.md](references/conventions.md) §Owner Boundaries).
- Don't invent `type`, `status` or `owner` values; the schema in `docs/tooling/frontmatter-schema.yaml` is the only source.

## Invocation Examples

```
/shop-spec
Write the reference architecture at docs/specs/platform/ref-architecture.md (owner platform)

/shop-spec
Create an ADR choosing between MetricFlow and the Boring Semantic Layer (owner analytics-eng)

/shop-spec
Write a runbook for a stuck Kafka Connect connector (owner platform)
```
