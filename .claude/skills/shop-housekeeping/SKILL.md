---
name: shop-housekeeping
description: Audits and repairs Shopstream docs under docs/specs/ and docs/adr/ for convention violations, banned vocabulary, broken references, status drift, and SSOT duplication. Triggers on "clean specs", "housekeep", "dedup", "compact docs", or a check after bulk doc edits.
---

# Shopstream Spec Housekeeping

References: [scan.md](references/scan.md) · [vocab.md](references/vocab.md)

SSOTs: [`docs/CONTRIBUTING.md`](../../../docs/CONTRIBUTING.md) · [`docs/tooling/frontmatter-schema.yaml`](../../../docs/tooling/frontmatter-schema.yaml) · [`docs/documentation-map.md`](../../../docs/documentation-map.md)

---

## Scope flags

| Flag              | Expands to                  |
| ----------------- | --------------------------- |
| `--platform`      | `docs/specs/platform/`      |
| `--ingestion`     | `docs/specs/ingestion/`     |
| `--transform`     | `docs/specs/transform/`     |
| `--streaming`     | `docs/specs/streaming/`     |
| `--orchestration` | `docs/specs/orchestration/` |
| `--ai`            | `docs/specs/ai/`            |
| `--refs`          | `docs/specs/ref/`           |
| `--adr`           | `docs/adr/`                 |
| `--all`           | the full `docs/` tree       |

Default: `--all`. A flag whose folder doesn't exist yet reports "nothing to scan".

---

## Step 0 — Dynamic Plan

Emit this before any scan:

```
SCOPE:  <resolved path(s)>
DOCS:   <count> .md files
DIMS:   lint · vocab · status · ssot · freshness
AGENTS: <N scan> + 1 triage + <M fix> + 1 gate
```

**Agent strategy:** parallel Agent calls, one scan agent per domain folder that exists, or a single agent for 15 docs or fewer. Don't use the Workflow tool unless the user explicitly asks for a workflow.

---

## Phase 1 — Scan

Spawn the scan agents in parallel (`scan:<domain>`). Each runs the checks in [scan.md](references/scan.md) against its target and returns:

```json
{ "domain": "platform", "block": [], "warn": [], "note": [] }
```

No fixes in Phase 1.

---

## Phase 2 — Triage

One agent merges every Phase 1 result and emits one line per finding:

```
BLOCK  docs/specs/platform/ref-architecture.md:42  — banned term "MinIO"
WARN   docs/specs/ingestion/trd-cdc-to-bronze.md:8  — depends-on path broken
NOTE   docs/adr/adr-001-version-matrix.md           — updated: older than the last commit
```

**Stop here if there are 0 findings.** Report clean.

---

## Phase 3 — Fix

Spawn fix agents in parallel, one per severity bucket:

| Agent       | Handles                                                    | Source                                                                  |
| ----------- | ---------------------------------------------------------- | ----------------------------------------------------------------------- |
| `fix:block` | Docs-lint failures, banned vocabulary                      | `uv run pytest tests/test_docs_integrity.py -q` output; [vocab.md](references/vocab.md) |
| `fix:warn`  | Broken relationship paths, status drift, SSOT duplication  | [scan.md](references/scan.md) §WARN                                     |
| `fix:note`  | Stale `updated:` dates                                     | [scan.md](references/scan.md) §NOTE                                     |

Fix rules:

| Finding                | Fix                                                                                                                      |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Banned vocabulary      | Replace per the [vocab.md](references/vocab.md) Banned Terms table, honoring its exceptions                             |
| Broken frontmatter ref | Resolve it to the correct path; remove it if the target was deleted                                                     |
| SSOT duplication       | Replace the block with `> See [<ssot-file>](<path>) — SSOT for <X>`                                                     |
| Status drift           | Frontmatter is authoritative. Flag the doc for its owner; never promote a status automatically                           |
| Stale `updated:`       | Set it to the date of the change being made                                                                              |

---

## Phase 4 — Quality Gate

For each changed doc:

1. Apply shop-spec Phase 7 compression (rules in [scan.md §Compression](references/scan.md#compression-rules-phase-7)).
2. If it's advancing Proposed → Accepted, run shop-spec Phase 6 (≥ 13/15).
3. Run `uv run pytest tests/test_docs_integrity.py -q`; it must pass.

---

## Example

```
> /shop-housekeeping --platform

SCOPE:  docs/specs/platform/
DOCS:   1 file
AGENTS: 1 scan + 1 triage + 1 fix (block) + 1 gate

Phase 1 scan…   ✓  (1 BLOCK, 0 WARN, 0 NOTE)
Phase 2 triage… ✓
Phase 3 fix…    ✓  1 vocabulary replacement
Phase 4 gate…   ✓  docs lint passed
```
