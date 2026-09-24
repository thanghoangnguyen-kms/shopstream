---
title: "ADR-000: Record architecture decisions"
type: adr
status: Accepted
owner: platform
decision: "Record every architecture decision as a MADR 4.0 ADR in docs/adr/ with docs-kit frontmatter, enforced by the docs lint; this ADR also records the Week 1 toolchain baseline"
created: 2026-09-24
updated: 2026-09-24
---

# ADR-000: Record architecture decisions

## Context and Problem Statement

Shopstream is built over 23 weeks by one engineer playing three owner roles, in a public repo that reviewers read cold. Load-bearing choices (catalog, writer split, toolchain) will be questioned by reviewers and revisited by the author months later. Without a record, choices get argued again or drift silently. How should decisions be recorded so they're reviewable, linkable from specs, and checked by CI?

## Decision Drivers

- A reviewer must see *why*, including the options that lost.
- Specs must link to decisions (`decided-by:`), and the link must resolve.
- CI must enforce the format; a convention nothing enforces is a suggestion.
- $0, and no tool outside the repo.

## Considered Options

- MADR 4.0 body under docs-kit frontmatter in `docs/adr/adr-NNN-<topic>.md`
- Plain MADR 4.0 (`NNNN-<title>.md`, MADR's own front matter)
- No ADRs: README notes and commit messages
- GitHub wiki or issues

## Decision Outcome

Chosen option: "MADR 4.0 body under docs-kit frontmatter", because it keeps MADR's options-and-confirmation structure while using the frontmatter, owner roles and status lifecycle that `tests/test_docs_integrity.py` already enforces for every spec. Three-digit numbers match the curriculum's ADR-001/002/003.

### Consequences

- Good, because every decision has an owner, a status and a one-line `decision:` that tooling can read.
- Good, because a spec's `decided-by:` link is checked on every CI run.
- Bad, because MADR tooling that expects `NNNN-` filenames or MADR front matter won't read these files unchanged.
- Bad, because an Accepted body is immutable, so a change of mind costs a new ADR.

### Confirmation

`tests/test_docs_integrity.py` fails the build on a duplicate or malformed ADR number, missing frontmatter, or an illegal `type`, `status` or `owner`. `just adr-new` assigns numbers from the filenames.

## Pros and Cons of the Options

### MADR 4.0 body under docs-kit frontmatter

- Good, because one schema covers ADRs and specs.
- Bad, because it deviates from stock MADR tooling.

### Plain MADR 4.0

- Good, because it's the standard and existing tools read it.
- Bad, because it adds a second frontmatter schema for the lint to know about.

### No ADRs

- Good, because it costs nothing now.
- Bad, because the reasoning is lost, and "why not X?" gets argued again.

### GitHub wiki or issues

- Good, because it's easy to edit.
- Bad, because it lives outside the repo: no review in PRs, no CI checks, no links from specs.

## More Information

**Week 1 toolchain baseline.** Recorded here so that ADR-001 (version matrix), ADR-002 (retention) and ADR-003 (erasure) keep the meanings the curriculum gives them.

| Decision              | Chosen                                                                 | Instead of                       | Why                                                                                                     |
| --------------------- | ---------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Hook runner + pinning | prek, with every Python tool pinned in `uv.lock`                       | classic pre-commit; no hooks     | One lockfile. Same `.pre-commit-config.yaml` format, so switching back costs nothing. Used by CPython, Airflow, FastAPI |
| Python                | 3.13                                                                   | 3.12, 3.14                       | PySpark 4.1, Airflow 3.3, pyiceberg 0.12, duckdb 1.5 and confluent-kafka 2.15 all support it (PyPI, 2026-09-24) |
| License               | Apache-2.0                                                             | MIT                              | Matches Iceberg, Spark, Kafka and dbt; explicit patent grant                                            |
| CI shape              | 4 required checks: `lint`, `test`, `secrets`, `pr-title`               | one job                          | Each failure is reported on its own                                                                     |
| gitleaks pinning      | version in the `justfile`, verified against the release's `checksums.txt` | per-platform hashes in the repo | Renovate can bump it without manual steps. Protects against corruption, not against a compromised release |
| Docs system           | docs-kit conventions + a pytest lint with mutation tests               | ad hoc docs                      | Conventions that fail the build don't drift                                                             |
