---
title: "Shopstream — Contributing Guide"
type: guide
status: Active
owner: platform
created: 2026-09-24
updated: 2026-09-24
description: Conventions for authoring and maintaining docs in this repository.
---

# Shopstream — Contributing Guide

Conventions for authoring and maintaining the documentation in `docs/`. No code lives under `docs/`. `tests/test_docs_integrity.py` enforces the mechanical rules on every CI run.

> **Style, length and diagram theming:** see [guide-doc-style.md](specs/guide/guide-doc-style.md).

## Template Selection

| You are documenting...                   | Use type   | Path pattern                                      |
| ---------------------------------------- | ---------- | ------------------------------------------------- |
| WHAT a data product delivers             | `prd`      | `docs/specs/<domain>/prd-<kebab>.md`              |
| HOW a pipeline, job or service is built  | `trd`      | `docs/specs/<domain>/trd-<kebab>.md`              |
| A point-in-time binding decision         | `adr`      | `docs/adr/adr-NNN-<topic>.md` (`just adr-new`)    |
| A normative data/topology SSOT           | `ref`      | `docs/specs/ref/ref-<kebab>.md`, or its domain    |
| A procedural how-to or runbook           | `guide`    | `docs/specs/<domain>/guide-<kebab>.md`            |
| Term definitions                         | `glossary` | `docs/specs/ref/glossary-<kebab>.md`              |

Runbooks are guides named `guide-runbook-<failure>.md` in the domain that owns the failure.

## Frontmatter Requirements

Every `.md` file in `docs/specs/` and `docs/adr/` opens with a YAML frontmatter block carrying all four required fields:

```yaml
---
title: "Human-readable title"
type: trd # prd | trd | adr | ref | guide | glossary
status: Draft # Draft | Proposed | Accepted | Active | Rejected | Deprecated | Superseded
owner: platform # platform | analytics-eng | governance
---
```

[`docs/tooling/frontmatter-schema.yaml`](tooling/frontmatter-schema.yaml) is the full schema, including optional fields and typed relationship keys. The lint reads its enums, so a value is legal only when the schema lists it.

## Owners

| Owner           | Owns                                                                                                 |
| --------------- | ---------------------------------------------------------------------------------------------------- |
| `platform`      | Compose stack, storage, catalog, Kafka and CDC, bronze, Spark jobs, Airflow, observability, CI       |
| `analytics-eng` | dbt silver and gold, the Kimball model, reconciliation, the semantic layer and metrics               |
| `governance`    | Identity and authorization, retention and erasure policy, data contracts, PII, compliance mapping    |

Shopstream is a solo project; one person plays all three roles. Each deliverable still has exactly one owner, and a handoff names both sides.

## Typed Relationships

| Key           | Meaning                                                                          | Use in             |
| ------------- | -------------------------------------------------------------------------------- | ------------------ |
| `implements:` | This doc implements requirements in the listed PRD(s)                            | trd, adr           |
| `decided-by:` | This doc's approach was ratified by the listed ADR(s)                            | trd, prd           |
| `supersedes:` | This doc replaces the listed doc(s)                                              | any                |
| `informs:`    | This doc provides context to the listed doc(s)                                   | adr, ref           |
| `depends-on:` | This doc requires the listed doc(s) to be stable                                 | trd, guide, adr    |
| `amends:`     | This doc changes one clause of the listed doc(s), which stay authoritative otherwise | adr            |
| `amended-by:` | Inverse of `amends:`, carried on the amended doc                                 | adr, trd, prd, ref |

Values are relative paths from the current doc, as a list or a single path:

```yaml
decided-by:
  - ../../adr/adr-001-version-matrix.md
depends-on: [../platform/ref-architecture.md]
```

## Required Sections by Type

### PRD (a data product)

- Objective and Consumers
- Functional Requirements
- Non-Functional Requirements: an SLI/SLO table covering freshness, completeness, latency, availability and security
- Acceptance Criteria in EARS format
- Out of Scope
- Open Questions

### TRD

- §1 Objective (attribute table)
- §2 Architecture: Data Flow, Control Flow, Failure and Replay Flow
- §3 Data Model / Contract: Topic and Schema, Table, Job, Config (include the ones that apply)
- §4 Risk Assessment
- §5 Testing Strategy (test pyramid; name the test or `just` recipe that proves each claim)
- §6 Operational Boundaries (✅ Always / ⚠️ Ask first / 🚫 Never)

### ADR (MADR 4.0 body)

- Context and Problem Statement
- Decision Drivers
- Considered Options
- Decision Outcome, with Consequences and Confirmation
- Pros and Cons of the Options
- More Information
- The body is immutable after `status: Accepted`; supersede it with a new ADR

### REF

- Overview (SSOT declaration + consumer list)
- Core definitions
- Precedence Rules

### GUIDE

- Overview (with Prerequisites)
- Procedure
- Troubleshooting
- Operational Boundaries

### GLOSSARY

- One table: Term, Definition, Defined by (a link to the doc that owns the term)

## Document History

Git is the history. Don't add a `Change Log`, `Changelog`, `Critique Score` or `Relationship Updates` section to a document; `tests/test_docs_integrity.py` fails the build on all four. Use `git log --follow <file>` to see how a document changed, and the typed frontmatter keys (`supersedes:`, `amended-by:`, …) to record how documents relate.

## EARS Acceptance Criteria Format

Every PRD Acceptance Criteria section uses EARS:

```
WHEN [trigger condition] THE SYSTEM SHALL [observable behavior]
IF [precondition] THEN THE SYSTEM SHALL [behavior]
THE SYSTEM SHALL [always-on requirement]
```

Examples:

```
WHEN a customer's address changes in Postgres THE SYSTEM SHALL close the current dim_customer version and open a new one whose valid_from is the source change timestamp
WHEN an erasure request is recorded in the ledger THE SYSTEM SHALL remove the subject from every Iceberg data file, manifest and Kafka topic within the erasure SLA
THE SYSTEM SHALL reject a producer schema that breaks BACKWARD compatibility at the registry
```

## Naming Rules

```
docs/specs/<domain>/<type>-<kebab>.md
  type   ∈ {prd, trd, ref, guide, glossary}
  domain ∈ {platform, ingestion, transform, streaming, orchestration, ai, ref, guide}
  ref/ and guide/ are buckets for cross-cutting docs (SSOTs, authoring guides).
ADRs stay flat: docs/adr/adr-NNN-<topic>.md (3 digits, unique repository-wide).
To extend: create a domain folder with its first doc; prefix the file by type.
```

- kebab-case only: no spaces, no underscores
- the type prefix always comes first; the lint checks it matches `type:`
- assign ADR numbers with `just adr-new "<title>"`, never by hand
- see the [specs taxonomy](specs/README.md) for the domain folders

## Boundaries

| Tier         | Rule                                                                                              |
| ------------ | ------------------------------------------------------------------------------------------------- |
| ✅ Always    | Give every new doc the four required frontmatter fields                                           |
| ✅ Always    | Run `just check` before opening a PR; the docs lint is part of it                                 |
| ✅ Always    | Use EARS for PRD Acceptance Criteria                                                              |
| ⚠️ Ask first | Rename or delete an existing doc (breaks cross-references)                                        |
| ⚠️ Ask first | Add a `type`, `status` or `owner` value to the schema                                             |
| 🚫 Never     | Put code under `docs/`                                                                            |
| 🚫 Never     | Restate the writer/reader split; link to [ref-architecture.md](specs/platform/ref-architecture.md) |
| 🚫 Never     | Edit an ADR body after `status: Accepted`                                                         |
