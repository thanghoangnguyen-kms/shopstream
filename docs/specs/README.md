---
title: "Shopstream Specifications Taxonomy"
type: guide
status: Active
owner: platform
created: 2026-09-24
updated: 2026-09-24
description: Canonical document domains, naming rules, and how to add a doc.
---

# Shopstream Specifications Taxonomy

Specs live in domain folders under `docs/specs/`. Each folder holds the PRDs, TRDs, REFs and guides for one area. Two cross-cutting buckets, `ref/` and `guide/`, hold SSOTs and authoring guides that span domains. ADRs stay flat under `docs/adr/`. The entry point is the [documentation map](../documentation-map.md); authoring conventions are in [CONTRIBUTING](../CONTRIBUTING.md) and [guide-doc-style](guide/guide-doc-style.md).

## Domain folders

A folder is created with its first document; git doesn't track empty folders.

| Folder           | What lives here                                                                    | First doc expected |
| ---------------- | ---------------------------------------------------------------------------------- | ------------------ |
| `platform/`      | The reference architecture; Compose stack, storage, catalog, security topology     | W1                 |
| `ingestion/`     | Kafka, schemas, CDC, bronze landing; their runbooks                                | W6                 |
| `transform/`     | dbt silver and gold, the Kimball model (bus matrix, SCD policy), reconciliation    | W3                 |
| `streaming/`     | Spark streaming and batch jobs, benchmarks' design notes                           | W13                |
| `orchestration/` | Airflow assets, maintenance, SLOs, rebuild and backfill                            | W15                |
| `ai/`            | Semantic layer, MCP server, eval design, threat model                              | W21                |
| `ref/`           | Cross-cutting SSOTs and glossaries                                                 | when needed        |
| `guide/`         | Cross-cutting authoring guides                                                     | W1                 |

## Naming convention

```
docs/specs/<domain>/<type>-<kebab>.md
  type   ∈ {prd, trd, ref, guide, glossary}
  ref/ and guide/ are buckets for cross-cutting docs (SSOTs, authoring guides).
ADRs stay flat: docs/adr/adr-NNN-<topic>.md (global sequential numbering).
```

## How to add a doc

1. Pick the matching `<domain>/` folder, or create it with this doc.
2. Name the file `<type>-<kebab>.md`. Runbooks are `guide-runbook-<failure>.md`.
3. Update the [documentation map](../documentation-map.md) only when the doc introduces a new canonical domain.
4. Follow [guide-doc-style](guide/guide-doc-style.md) for frontmatter, EARS, `§N` numbering and diagrams, then run `just check`.
