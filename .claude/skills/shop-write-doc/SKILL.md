---
name: shop-write-doc
description: Analyzes, plans, writes, rewrites, reviews, and validates Shopstream design documents (PRDs, TRDs, ADRs, REFs, GUIDEs, GLOSSARY) under docs/specs/ and docs/adr/. Use for evidence-first document creation, focused review, style and tone cleanup, AI-like prose removal, contract and naming consistency, structural compression, Mermaid diagrams, status or version changes, and repository-grounded design updates. Do not use for general prose outside Shopstream design documentation or for implementation work that only happens to mention a document.
---

# Shopstream Design Document Writer

Produce an implementable Shopstream design document whose claims, terminology, ownership, diagrams, and validation evidence agree with the current repository.

## When to Use

Invoke this skill when:

- Rewriting or substantially revising an existing doc in `docs/specs/` or `docs/adr/`; for a new doc, prefer `/shop-spec`
- Doing a focused review: style cleanup, AI-prose removal, naming normalisation, compression
- Updating doc status, version, or relationship keys after an implementation change
- User says "write a spec", "update this doc", "review this PRD", "clean up this TRD", "add diagrams", "fix the doc"

**Example invocations:**

```
/shop-write-doc
Write a TRD for the CDC-to-bronze pipeline

/shop-write-doc
Review and clean up docs/specs/platform/ref-architecture.md — remove AI prose, compress
```

Use this loop:

```text
Intent -> Evidence -> Plan -> Draft -> Proof
```

Do not skip Proof. A polished document with an invalid contract, broken link, or misleading diagram is incomplete.

## Live authorities

Read current repository instructions before applying remembered conventions. For Shopstream docs, inspect these when present:

1. `AGENTS.md` (including §6 Platform gotchas) and directory-specific instructions.
2. `docs/CONTRIBUTING.md` for document types, names, sections, and lifecycle rules.
3. `docs/specs/guide/guide-doc-style.md` for writing, length, and diagram rules.
4. `docs/tooling/frontmatter-schema.yaml` for frontmatter and relationship keys.
5. Accepted ADRs, canonical REFs, contracts, manifests, schemas, tests, and deployed code relevant to the design.

Treat the user-approved outcome as the scope authority. Treat accepted repository artifacts as technical authority. If authorities conflict, expose the conflict and resolve it before writing a load-bearing claim.

## Route references

| Open when you need to...                                                                     | Read                                    |
| -------------------------------------------------------------------------------------------- | --------------------------------------- |
| frame the outcome, gather evidence, close decisions, or build a safe plan                    | `references/evidence-and-planning.md`   |
| set the audience, professional tone, information order, sentence style, or copyedit standard | `references/style-and-tone.md`          |
| write contracts, normalize terminology, remove feedback, or compress a long draft            | `references/writing-and-compression.md` |
| decide whether a diagram helps, choose a Mermaid type, or review diagram abstraction         | `references/diagram-design.md`          |
| review the finished document and run validation in a fix-and-retry loop                      | `references/review-and-validation.md`   |
| calibrate behavior from complete, guarded, and corrected examples                            | `references/examples.md`                |

Read only the references needed for the request, but always use the workflow below.

## Workflow

### 1. Frame intent and proof

Classify the request as `create`, `rewrite`, `focused update`, `review`, or `audit`.

Record privately:

- the owner outcome;
- the audience and decisions the document must enable;
- files in scope and files that must remain untouched;
- the observable completion proof;
- the most likely way to produce a convincing but wrong document.

For an existing document, preserve its accepted decisions unless the user explicitly authorizes a change. For review-only requests, report findings without editing.

### 2. Gather evidence before prose

Read the target document completely. For long documents, map headings, tables, diagrams, examples, requirements, risks, and tests before editing.

Inspect related canonical documents and implementation evidence: code, tests, schemas, contracts, Compose and CI config. Use `rg` for exact terms and files after the architectural path is known.

Build a working ledger of:

- decisions and unresolved branches;
- canonical terms and retired aliases;
- identifiers, keys, buckets, subjects, paths, enums, and placeholders;
- owners and system boundaries;
- source-of-truth links;
- risks, failure behavior, and required proof.

Do not infer current behavior from a plan or stale draft when code, tests, schemas, or accepted documents can answer it.

### 3. Close material decisions

Answer from repository evidence when possible. Ask the user only when an unresolved choice changes scope, ownership, compatibility, security, lifecycle, or acceptance.

Ask one question at a time. Include a recommendation and one short reason. Integrate the answer before asking the next question. Stop when remaining choices are implementation details or explicitly deferred.

### 4. Plan the document change

Define:

- target shape and section ownership;
- contract changes and compatibility impact;
- diagrams to add, replace, link, or remove;
- duplicated or non-normative content to compress;
- status, version, and relationship changes;
- validation commands and acceptance gates;
- the exact allowed file set.

Validate this plan against the evidence ledger before editing. Preserve unrelated working-tree changes. Do not broaden a focused rewrite into related-document edits without authorization.

### 5. Write in reviewable passes

Before drafting, identify the primary reader, what they must decide or implement, and the detail they need. Read `references/style-and-tone.md` for every create or rewrite request and for any review that includes style, clarity, tone, or concision.

Order technical communication as:

1. What: state the decision, contract, or outcome.
2. Why: name the constraint, risk, or tradeoff.
3. How: describe ownership and behavior.
4. Proof: state how the claim is tested or observed.

Use this order as a reader model, not a rigid paragraph template.

Write section by section in dependency order:

1. objective and decision;
2. architecture and ownership;
3. normative contracts;
4. risks and failure behavior;
5. tests and acceptance proof;
6. operational boundaries and references.

Use the live document-type rules instead of a remembered template. Keep one canonical definition per concept and link to external authorities instead of copying them. Repeat only the consequence needed for a risk or test to remain understandable.

Preserve meaning before polishing style. Use active voice, positive statements, concrete terms, one topic per paragraph, and parallel grammar. Prefer fewer, stronger edits over mechanical rewriting. Remove reviewer feedback, drafting comments, resolved questions, TODO markers, process narration, puffery, and AI-like filler from the final design unless they are intentional open decisions with an owner and deadline.

### 6. Add only useful diagrams

Add a diagram when it makes a multi-component relationship, temporal flow, lifecycle, or data model materially easier to understand. Keep diagrams high-level, canonical, and safe for the intended audience.

Read `references/diagram-design.md` before adding or materially changing Mermaid. Keep exact keys, subjects, fields, credentials, and deployment internals in prose or contract tables unless the diagram specifically exists to explain them.

### 7. Review and validate

Run separate passes for structure, technical authority, contracts, failure behavior, writing, diagrams, and mechanics. Then run the repository's current documentation checks.

Fix each failure and rerun the failed check. Do not report a check as passing unless it ran successfully. If a broad check has a pre-existing failure, record the exact failure and provide focused evidence for the changed document.

### 8. Deliver the result

Lead with the outcome. Report:

1. files changed;
2. material decisions and contract corrections;
3. validation results;
4. unresolved gaps or decisions.

Keep the handoff concise. Do not repeat the document.

## Non-negotiable boundaries

- Preserve unrelated user changes.
- Do not edit an Accepted ADR body. Propose a successor decision when the repository lifecycle requires one.
- Do not invent names, owners, statuses, schema fields, service boundaries, capacities, or validation results.
- Do not duplicate canonical topology or contract definitions when a stable reference owns them.
- Do not hide a compatibility break behind a patch-level version or a wording-only commit message.
- Do not use rendered diagram images as the source of truth. Keep Mermaid or ASCII in the document.
- Do not mark the work complete without evidence tied to the original outcome.
