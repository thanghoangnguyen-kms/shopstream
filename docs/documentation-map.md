# Shopstream Documentation Map

The stable domain map for the repository documentation. This page doesn't duplicate every title, status or summary; the directory tree is the inventory.

## Start here

- [Documentation home](README.md): paths into the docs by audience.
- [Reference architecture](specs/platform/ref-architecture.md): system topology, boundaries, canonical flows, data ownership, technology choices and invariants.
- [Contribution guide](CONTRIBUTING.md): frontmatter, owners, naming, required sections and review rules.
- [Docs layout for agents](claude/docs-layout.md): where each domain lives and the status lifecycle.
- [ADR-000](adr/adr-000-record-architecture-decisions.md): how decisions are recorded, plus the Week 1 toolchain baseline.

## Canonical product and engineering documents

- `specs/platform/`: the [reference architecture](specs/platform/ref-architecture.md) and platform-wide topology.
- `specs/guide/`: the [style and authoring guide](specs/guide/guide-doc-style.md).
- `specs/<domain>/`: one folder per area (ingestion, transform, streaming, orchestration, ai), each created with its first doc; see the [taxonomy](specs/README.md).
- `adr/`: architecture decisions; numbers are unique repository-wide.
- `tooling/`: the frontmatter schema the lint reads, and the ADR template.

## Operations and assurance

- `evidence/`: Prove-step evidence per week (W1 onwards).
- `benchmarks/`: Kafka (W7) and Spark (W14) benchmark reports.
- `eval/`: the text-to-SQL evaluation (W22).

## Retention policy

- Current operational guidance, contracts, requirements and implementation-facing decisions remain in this tree.
- **Proof artifacts in `evidence/`, `benchmarks/` and `eval/` are kept permanently.** They are the portfolio's evidence, not research.
- Dated research, completed reviews, generated planning snapshots, superseded designs and duplicate handoffs are removed once their conclusions are absorbed.
- Git history is the archive for deleted rationale; don't recreate a parallel `archive/` or `research/` tree.
- Add a new document only when an existing canonical page can't own the content. The directory tree is the inventory; don't expand this file into a second registry.
