# Shopstream Spec — Critique Protocol

## When to Invoke

| Transition          | Critique required?                                                            |
| ------------------- | ----------------------------------------------------------------------------- |
| Draft → Proposed    | Opt-in (recommended for PRDs, TRDs and the reference architecture); ≥ 11/15   |
| Proposed → Accepted | **Mandatory**; aggregate ≥ 13/15                                              |

---

## How to run it

Run the three critics as **independent subagents**, dispatched in parallel with one Agent call each. Give each critic only:

1. the path of the document under review
2. this file, and the name of its critic section
3. read access to the repo; the critic reads `AGENTS.md` §6, `docs/specs/platform/ref-architecture.md` and any linked doc it needs

Each critic returns scores for all five dimensions plus its flags. Critics never edit the document, and the writer is never a critic.

## 5-Dimension Rubric

Score each dimension 1–3 (5 × 3 = 15).

| #     | Dimension             | 1: Failing                                             | 2: Adequate                                           | 3: Strong |
| ----- | --------------------- | ------------------------------------------------------ | ----------------------------------------------------- | --------- |
| **C** | **Completeness**      | Required sections missing or placeholder text present  | All sections present; some thin but not blocking      | All sections substantive; zero placeholder lines |
| **I** | **Implementability**  | Needs out-of-band knowledge to build                   | Some ambiguity, resolvable with follow-up questions   | Buildable from the doc and its links alone |
| **T** | **Testability**       | Claims can't be falsified; no test named               | Some claims map to tests; some NFRs lack bounds       | Every AC, NFR and invariant names the test, CI gate or `just` recipe that proves it |
| **R** | **Risk Coverage**     | §4 absent or generic                                   | Some data-platform failure modes addressed            | Late, duplicate and out-of-order data, schema drift, replay/backfill idempotency, erasure interaction and laptop resource limits are each addressed where they apply |
| **O** | **Owner Attribution** | Owners missing or wrong                                | Most deliverables attributed; handoffs unclear        | Every deliverable has one owner role matching `conventions.md` Owner Boundaries, and every handoff names both sides |

The aggregate uses, for each dimension, the **lowest** score any critic gave.

---

## Shopstream Critic Roles

### Data Correctness Critic

**Domain:** grain, keys, CDC semantics, SCD2, late-arriving data, reconciliation.

1. Is the grain of every table declared, and do the joins preserve it (no fan-out, no silent dedup)?
2. Are CDC changes applied in source order (LSN or source timestamp, never ingestion time), deletes included?
3. Is SCD2 validity taken from source timestamps, and is non-overlap tested?
4. Are late-arriving dimensions handled with inferred members that are corrected later?
5. Is there a reconciliation against the source with a stated tolerance, and does the doc say what happens when it fails?

### Reliability & Operability Critic

**Domain:** failure modes, idempotency, replay, SLOs, runbooks, resource budget.

1. What happens to late, duplicate and out-of-order events, and is each case tested?
2. Is every write idempotent under retry and replay? Would a rebuild from Kafka offsets and bronze snapshots produce identical row hashes?
3. What does a breaking schema change do (registry rejection, a DLQ, a failed contract gate), and where is that tested?
4. Which SLOs (freshness, lag, DLQ depth, failed runs) apply, and which alert fires when one breaches?
5. Is there a runbook for each failure mode the doc introduces, and does the design fit the Compose profiles' RAM budget?

### Security & Privacy Critic

**Domain:** identity, least privilege, PII locations, erasure, secrets, agent-facing access.

1. Does each component use its own identity with least privilege (an OIDC client plus OpenFGA grants), and is there an access-denied test?
2. Where can PII land: manifest column bounds, DLQ payloads, Kafka segments, Debezium before-images, logs, backups? Is each location covered?
3. Does the design keep erasure possible end to end (ledger, DELETE, rewrite, expire, orphan removal), including after a replay or a restore?
4. Are secrets kept out of git, config files and logs?
5. For anything agent-facing: can the agent's identity read only gold, and is prompt injection through data (the seeded review) tested?

---

## Debate Protocol

1. **Independent review:** each critic scores all five dimensions and records flags as `[<Critic>] Q<N>: <one-line finding>`.
2. **Cross-examination:** flags that touch shared ground (for example, replay idempotency and erasure) go to all three critics.
3. **Writer response:** for each flag, the writer either edits the doc or logs it in the doc's Open Questions table with an owner role and a target week. REF and ADR docs without an Open Questions section log it in the PR description.
4. **Re-score:** critics re-score only the dimensions a resolution touched.

---

## Synthesis Format

The synthesis goes in the **PR description** (or the chat, for a local review), never in the document; the docs lint fails the build on a critique heading.

```text
Critique result: <doc-slug> (<YYYY-MM-DD>)

| Dimension         | Score | Flags |
| ----------------- | ----- | ----- |
| Completeness      | /3    |       |
| Implementability  | /3    |       |
| Testability       | /3    |       |
| Risk Coverage     | /3    |       |
| Owner Attribution | /3    |       |
| Aggregate         | /15   |       |

Gate: Draft → Proposed ≥ 11 · Proposed → Accepted ≥ 13
Unresolved flags: <list, or "none">
Recommendation: Advance / Hold pending flags <N, M>
```
