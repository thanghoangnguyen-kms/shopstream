# Contract Writing and Compression

Read this reference when structuring normative content, normalizing contracts, removing review residue, or reducing a long design document. Read `references/style-and-tone.md` for sentence style, professional tone, and copyediting.

## Contents

1. Separate normative content from explanation
2. Normalize the contract ledger
3. Preserve authority and ownership
4. Remove review residue
5. Compress without losing proof
6. Run section-by-section review

## 1. Separate normative content from explanation

| Content                            | Best shape                                        |
| ---------------------------------- | ------------------------------------------------- | ---------------------- | ------------ | ---------- | ------------ |
| Decision or invariant              | Short declarative paragraph                       |
| Field, identifier, owner, or bound | Table                                             |
| Payload or schema                  | One representative valid example plus field rules |
| Ordered behavior                   | Numbered steps or sequence diagram                |
| Alternatives                       | `Alternative                                      | Why rejected` table    |
| Risk                               | `Risk                                             | Likelihood             | Impact       | Mitigation | Proof` table |
| Test                               | `Scenario                                         | Expected result` table |
| Operational permission             | `Always                                           | Ask first              | Never` table |

Use `MUST`, `SHALL`, or repository-specific normative language only for enforceable requirements. Name the enforcement point or test where practical.

## 2. Normalize the contract ledger

Scan the entire document for every contract-bearing token:

- IDs and selectors;
- bucket and key names;
- subjects and event types;
- API paths and methods;
- database entity IDs;
- environment variables;
- enums, severities, states, and timestamps;
- schema versions and capability names;
- owner names and component names;
- pattern variables and concrete examples.

For each concept, select one canonical term. Replace aliases, update examples, and add a retired-term scan to validation. Do not rename a contract only in prose. Trace the change through diagrams, tables, payloads, risks, tests, operations, and references.

Keep distinct concepts distinct. For example, a rule-type selector may determine evaluation behavior while a use-case ID remains an event namespace. Similar values do not make the fields interchangeable.

## 3. Preserve authority and ownership

- State who writes, validates, reads, and operates each contract.
- Separate source-of-truth ownership from transport or deployment responsibility.
- Link to canonical topology, vocabulary, or field-mapping documents instead of copying their full definitions.
- Use current code or schema names in technical contracts. Use friendly labels only in explanatory prose.
- State proposed behavior as proposed. Do not describe an unbuilt component as deployed.

When two sources conflict, keep both facts visible in working notes, choose the higher authority, and record the required migration or unresolved decision in the document.

## 4. Remove review residue

Delete these from a professional final design unless the repository explicitly requires them:

- reviewer comments and inline feedback;
- resolved questions and conversational answers;
- TODO, TBD, FIXME, draft notes, and placeholder text;
- statements such as "we should discuss" without an owner and deadline;
- self-review scores embedded in normative sections;
- explanations of how the author edited the document.

Keep a genuine open question only when it blocks a later decision but not the current proposal. Give it an owner, deadline or decision gate, and stated impact.

## 5. Compress without losing proof

For a long document, map before editing:

1. headings and their purpose;
2. tables and the facts they own;
3. diagrams and the relationships they own;
4. examples and the schema shapes they prove;
5. repeated contracts, risks, and tests.

Then apply these cuts:

- Delete prose that reads the table directly above it.
- Replace repeated definitions with a link to the canonical definition.
- Merge adjacent sections that answer the same reader question.
- Replace several similar payloads with one representative example that retains every normative shape.
- Remove decorative headings and preambles.
- Keep only the current change-log window required by the style guide.
- Split detail into a canonical REF only when the user authorizes a new file and the detail has independent consumers.

Do not remove intentional traceability. Risks and tests may repeat a contract's consequence, but they should not redefine the contract.

## 6. Run section-by-section review

Review in this order:

1. Objective: one decision, scope, non-goals, and ownership are clear.
2. Architecture: components and boundaries match deployed or explicitly proposed reality.
3. Contracts: names, types, examples, lifecycle, compatibility, and failure behavior agree.
4. Risks: each material gap or unbuilt dependency has a mitigation and proof plan.
5. Testing: scenarios can falsify the contract and cover failure paths.
6. Operations: cold start, hot update, rollback, degraded state, and authority are explicit.
7. References: links resolve and breaking decisions are visible.

After the section passes, read the full document once for flow and duplication.
