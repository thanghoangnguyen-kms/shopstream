# Evidence and Planning

Read this reference to turn a document request into a repository-grounded, bounded plan.

## Contents

1. Frame the outcome
2. Select the operating mode
3. Gather evidence in passes
4. Build the evidence ledger
5. Close the decision tree
6. Create and validate the plan

## 1. Frame the outcome

Capture this intake privately. Do not turn it into process text in the document.

| Field            | Question                                                          |
| ---------------- | ----------------------------------------------------------------- |
| Original request | What did the user ask to become true?                             |
| Audience         | Who must understand or implement the design?                      |
| Decision         | What choice or contract must the document make clear?             |
| Scope            | Which files and systems may change?                               |
| Non-goals        | What must remain untouched?                                       |
| Authority        | Which choices are requested, accepted, inferred, or unresolved?   |
| Oracle           | What observable evidence proves the final document is correct?    |
| Likely misfire   | How could the work look complete while solving the wrong problem? |

A good oracle combines an artifact with checks. Example: the target TRD contains the accepted contract, stays within its line limit, renders its diagrams, passes docs tests and build, and leaves unrelated files unchanged.

## 2. Select the operating mode

| Mode           | Default behavior                                                                                                     |
| -------------- | -------------------------------------------------------------------------------------------------------------------- |
| Create         | Check for an existing canonical doc, select the live template, then write a new file and required registration only. |
| Rewrite        | Preserve accepted meaning, map the whole document, then restructure and compress within the authorized file set.     |
| Focused update | Trace the changed decision through contracts, risks, tests, operations, and references.                  |
| Review         | Return prioritized findings with evidence and suggested corrections. Do not edit.                                    |
| Audit          | Evaluate compliance and consistency. Keep the pass read-only unless fixes are explicitly authorized.                 |

## 3. Gather evidence in passes

Use the minimum passes needed, but cover every high-impact dimension.

| Pass                | Inspect                                                                  | Output                                 |
| ------------------- | ------------------------------------------------------------------------ | -------------------------------------- |
| Repository rules    | Instructions, contributing guide, style guide, schema                    | Required shape and gates               |
| Current artifact    | Entire target doc, frontmatter, links, diagrams, examples                | Structural map and current claims      |
| Canonical decisions | Accepted ADRs and REFs                                                   | Binding choices and ownership          |
| Runtime truth       | Code, schemas, manifests, tests, Compose and CI config            | Implemented behavior and impact        |
| Related docs        | Same services, identifiers, contracts, or relationships                  | Duplication, drift, and dependency map |
| History             | Git log, blame, review context when the decision depends on past changes | Reason for non-obvious behavior        |
| Negative evidence   | Failures, rejected alternatives, retired names, stale patterns           | Risks and prohibited regressions       |

Stop gathering when high-impact dimensions are complete, remaining gaps are explicit, and new sources mostly repeat known facts.

## 4. Build the evidence ledger

Use a compact table while working:

| Claim or term   | Canonical value    | Authority                             | Consumers               | Change needed                   |
| --------------- | ------------------ | ------------------------------------- | ----------------------- | ------------------------------- |
| Document status | `Proposed`         | frontmatter schema plus user decision | frontmatter             | preserve                        |
| Config selector | `<canonical name>` | contract schema                       | env table, flow, tests  | replace aliases                 |
| Event subject   | `<pattern>`        | topology REF                          | contract, example, test | link, do not duplicate topology |

Add rows for ownership, lifecycle, failure semantics, security boundaries, capacities, and version compatibility when relevant.

Distinguish three facts:

- Pattern: contains variables and states allowed shape.
- Concrete example: contains rendered values and proves readability.
- Runtime value: comes from deployment or configuration evidence.

Never mix these forms in one row without labeling them.

## 5. Close the decision tree

Order unresolved choices by dependency. Resolve the root decision first.

Ask only if the answer changes one of these:

- public contract or compatibility;
- writer or owner authority;
- lifecycle or deletion behavior;
- security, privacy, or safety boundary;
- capacity or availability commitment;
- status, version, or supersession;
- files in scope;
- acceptance proof.

Use this question shape:

```text
Question: <one precise decision>?
Recommendation: <preferred option>, because <one evidence-based reason>.
Alternatives: <only viable alternatives>.
```

Do not ask what the repository already answers. Do not ask several dependent questions together.

## 6. Create and validate the plan

The plan must name:

1. the outcome and oracle;
2. evidence authorities;
3. exact files allowed to change;
4. structural changes;
5. contract and terminology changes;
6. diagrams and their purpose;
7. risks and compatibility treatment;
8. checks to run;
9. stop conditions.

Before editing, challenge the plan:

- Does every breaking decision reach the contract, risk, test, operations, and version sections?
- Does a canonical REF already own any proposed duplicated content?
- Does the plan preserve unrelated worktree changes?
- Can each acceptance claim be observed or tested?
- Would a different reader infer a second meaning from any key term?

Revise the plan until these checks pass. Then edit only the allowed files.
