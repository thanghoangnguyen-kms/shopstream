# Examples

Read these examples to calibrate the workflow. Adapt the behavior, not the literal names.

## 1. Happy path: create a focused TRD

**Request**

```text
Write a TRD for a coordinator-to-partner configuration integration.
```

**Weak behavior**

- Starts drafting from memory.
- Copies a generic TRD template.
- Invents a bucket, event subject, and owner.
- Adds a decorative architecture diagram.
- Reports completion after proofreading.

**Target behavior**

1. Read repository instructions, the current document schema, style guide, topology REF, contract schema, and related ADRs.
2. Define the outcome and proof: the partner can implement the integration, examples parse, diagrams render, and docs checks pass.
3. Build a ledger for writers, readers, IDs, keys, subjects, lifecycle, retries, compatibility, and ownership.
4. Ask only unresolved contract decisions, one at a time with a recommendation.
5. Plan the exact file, sections, diagrams, and validation gates.
6. Write one canonical definition per contract and link to topology details.
7. Add an overview only if it clarifies three or more boundaries, plus a sequence only if timing and retries matter.
8. Validate frontmatter, links, examples, Mermaid, line count, docs tests, docs build, diff, and worktree scope.

## 2. Guarded rewrite: breaking integration proposal

**Request**

```text
Rewrite this oversized Proposed TRD. Change its key layout, publisher, and output ownership. Preserve unrelated worktree changes.
```

**Target behavior**

1. Snapshot `git status --short` and allow only the target document.
2. Map the whole draft before editing. Identify repeated definitions, table re-reading, oversized examples, feedback comments, and stale names.
3. Confirm that the requested changes are breaking. Apply the repository's compatible version and status treatment instead of presenting them as wording edits.
4. Trace each decision through architecture, contracts, risks, tests, operations, and references.
5. Keep the partner-facing integration diagrams high-level. Put exact subjects, keys, headers, and payload fields in contract tables.
6. Preserve distinct concepts. A runtime rule selector does not become an event namespace merely because both identify a use case.
7. Add explicit risks for unbuilt workers, new publishers, migration, non-zero recovery-point objectives, best-effort cleanup, and unvalidated capacity when evidence requires them.
8. Run the complete proof loop and compare the final worktree with the initial snapshot.

**Security variant**

If the rewrite changes direct device control or writer ownership, document fencing before reassignment: revoke the old writer's access, verify device state manually, then enable the new writer. Do not include credentials or private network values in examples or diagrams.

## 3. Anti-pattern and correction

**Anti-pattern**

```text
Use USE_CASE_ID everywhere for consistency. Repeat the full Kafka topic settings in the TRD so it is self-contained. Put the exact bucket, key, subject, headers, and JSON fields in one integration diagram. Keep reviewer notes at the end for transparency. The docs build passed last week, so no need to rerun it.
```

**Why it fails**

- It merges identifiers with different meanings.
- It creates a second topology authority that will drift.
- It makes the diagram dense and leaks detail that belongs in contracts.
- It leaves drafting residue in a professional design.
- It claims stale validation as current proof.

**Corrected behavior**

```text
Keep one canonical name per concept and record how each is derived. Link to the topology REF and state only this integration's subject contract. Use a high-level component or sequence diagram, then put exact values in tables and one validated example. Remove resolved feedback and retain only owned open decisions. Run the current docs tests and build after the final edit.
```
