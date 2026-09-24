# Shopstream Spec — Conventions Reference

Authoritative sources (reference them; don't duplicate them):

- `docs/tooling/frontmatter-schema.yaml`: field schema, owner roles, status transitions, relationship keys
- `docs/CONTRIBUTING.md`: required sections per type, EARS examples, naming rules
- `docs/documentation-map.md`: the canonical-domain map
- `AGENTS.md` §6: the Platform gotchas

---

## ADR Sequential Numbering

Derive the number from the filenames every time:

```
ls docs/adr | sort | tail -1
```

Or run `just adr-new "<title>" [owner]`, which assigns the highest number + 1, zero-padded to 3 digits. Don't record a "last known" number anywhere; it goes stale.

---

## Owner Boundaries

| Owner           | Responsible for                                                                                                                                                  | Not responsible for                       |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `platform`      | Compose stack, SeaweedFS, Lakekeeper, Kafka, Karapace, Debezium, the Kafka Connect sink (bronze), Spark jobs (sessions, affinity mart, WAP, maintenance, erasure DELETE), Airflow, observability, CI and supply chain | dbt models, metric definitions, access-policy decisions |
| `analytics-eng` | dbt silver and gold, the Kimball design (bus matrix, SCD2, facts), reconciliation, the semantic layer and metrics, MCP tool definitions                          | Infrastructure, retention and erasure policy |
| `governance`    | OIDC clients and OpenFGA grants, retention (ADR-002) and erasure (ADR-003) policy, ODCS contracts and PII classification, compliance mapping                     | Pipeline implementation                   |

Boundary rule: each deliverable has exactly one owner. A handoff names both sides and the artifact that crosses. For example, `governance` defines the erasure ledger, and `platform` and `analytics-eng` apply it.

---

## Platform-gotcha audit (Phase 5)

Check the produced doc against every row of `AGENTS.md` §6. For each claim that contradicts a gotcha, flag `G<N>: <claim> → <correct statement>` and fix the doc. Before using a gotcha older than about three months to reject a claim, re-verify it against a primary source (release notes, docs, the issue tracker), and update `AGENTS.md` if it changed.

End every audit with `uv run pytest tests/test_docs_integrity.py -q`.

---

## Naming Rules (from CONTRIBUTING.md)

- `kebab-case` only: no spaces, no underscores
- The type prefix comes first: `prd-`, `trd-`, `ref-`, `guide-`, `glossary-`; the lint checks it matches `type:`
- ADRs: `adr-NNN-<topic>.md`, 3 digits
- Check `docs/specs/` and `docs/adr/` for conflicts before proposing a slug

## Status Rules

- New documents start at `status: Draft`.
- ADR bodies are immutable after `status: Accepted`; supersede them with a new ADR.
- `Rejected` is for decisions that were considered and declined; keep the file.
- `Active` is for `ref`, `guide` and `glossary` docs in use; not for `prd`, `trd` or `adr`.

---

## Relationship Sweep Protocol (Phase 7b)

### Step 1 — Find impacted docs

```bash
# Docs that mention the same component, table or topic
grep -rl --include='*.md' '<component-or-table-or-topic>' docs/

# Docs that cite the same ADR(s) as this doc's frontmatter
grep -rl --include='*.md' 'adr-NNN' docs/
```

### Step 2 — Classify the relationship

| Situation                                                        | Key to add                                                                 | Direction                                     |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------- |
| New TRD implements an existing PRD                               | `implements: [<prd>]` on the new TRD                                       | —                                             |
| New TRD depends on an existing REF (e.g. `ref-architecture.md`)  | `depends-on: [<ref>]` on the new TRD                                       | REF may gain `informs:`                       |
| New ADR ratifies a choice in an existing TRD or PRD              | `decided-by: [<adr>]` on the existing doc                                  | ADR may gain `informs:`                       |
| New doc supersedes an existing doc                               | `supersedes: [<old>]` on the new doc; `status: Superseded` on the old one  | —                                             |
| New doc gives context an existing doc should know about          | `informs: [<existing>]` on the new doc                                     | Existing doc gains `depends-on:` if normative |

### Step 3 — Emit and apply the patch list

Put the list in the PR description (or the chat), never inside the document:

```text
PATCH docs/specs/<domain>/<slug>.md   ADD depends-on: [<new-doc-path>]
PATCH docs/adr/<adr-slug>.md          ADD informs: [<new-doc-path>]
```

Apply every patch in the same PR, then re-run `uv run pytest tests/test_docs_integrity.py -q`. Touch `docs/documentation-map.md` only when the document introduces a canonical domain.
