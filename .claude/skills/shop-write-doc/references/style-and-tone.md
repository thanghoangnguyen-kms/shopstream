# Style and Tone

Read this reference when writing or reviewing Shopstream prose for audience fit, professional tone, clarity, concision, or natural language.

## Contents

1. Set the audience and purpose
2. Put information in reader order
3. Build clear sentences and paragraphs
4. Use a professional technical tone
5. Remove AI-like writing without flattening the voice
6. Format for scanning
7. Distinguish facts, proposals, and requirements
8. Apply focused rewrites
9. Run the copyedit pass

## 1. Set the audience and purpose

Before writing, answer:

1. Who is the primary reader?
2. What must that reader decide, implement, review, or operate?
3. What detail is necessary for that action?
4. Why does the section matter to that reader?

Calibrate the document to its primary reader while keeping contracts exact.

| Reader                   | Emphasize                                                               |
| ------------------------ | ----------------------------------------------------------------------- |
| Portfolio reviewer       | Decision, evidence, proof artifacts, and what was deliberately left out |
| Shopstream implementer   | Exact contracts, lifecycle, compatibility, dependencies, and tests      |
| Architecture reviewer    | Decision, alternatives, tradeoffs, risks, and authority                 |
| Operator                 | Trigger, observable state, safe action, recovery, and escalation        |
| Delivery or product lead | Outcome, scope, dependencies, risk, and completion proof                |

Define an uncommon acronym at first use. Prefer a common developer term when it remains accurate. Do not simplify away a contract distinction.

## 2. Put information in reader order

Lead with the key point. Use this sequence when it fits:

| Order | Reader question               | Content                                         |
| ----- | ----------------------------- | ----------------------------------------------- |
| What  | What is true or changing?     | Decision, rule, contract, or outcome            |
| Why   | Why is this design necessary? | Constraint, evidence, tradeoff, or risk         |
| How   | How does it work?             | Ownership, interaction, lifecycle, or procedure |
| Proof | How will we know it works?    | Test, metric, validation, or operational signal |

Do not make the reader cross several background paragraphs to find the decision. Put background after the decision unless the background is required to understand it.

## 3. Build clear sentences and paragraphs

Apply these rules:

- Use active voice. Name the actor that performs the action.
- Put statements in positive form. State what the system does.
- Use definite, specific, concrete language.
- Keep the subject, verb, object, and modifiers close together.
- Use one tense within a summary or flow.
- Use parallel grammar for parallel requirements and table rows.
- Put one topic in each paragraph and open with its main point.
- End a sentence on the detail that deserves emphasis.
- Remove words that do not change meaning.

Prefer short sentences for rules and longer sentences only when the relationship between clauses matters. Split a sentence if the reader must hold more than one condition, exception, and outcome at once.

## 4. Use a professional technical tone

Write with these balances:

| Prefer                         | Avoid                                           |
| ------------------------------ | ----------------------------------------------- |
| Confident and evidence-based   | Absolute claims without proof                   |
| Direct and respectful          | Defensive, apologetic, or conversational filler |
| Plain and technically exact    | Academic, legalistic, or promotional language   |
| Friendly developer terminology | Internal jargon the partner team cannot decode  |
| Explicit ownership             | Vague actors such as "we" or "the solution"     |
| Honest proposal status         | Describing planned behavior as deployed fact    |

Avoid "obviously", "simply", "easy", and similar words that dismiss implementation cost. Do not use rhetorical questions, sales language, praise, or emotional emphasis in normative sections.

## 5. Remove AI-like writing without flattening the voice

Meaning comes first. Keep a sentence that is already accurate, natural, and consistent. Over-editing is failure because it can erase deliberate distinctions or the author's technical voice.

Prefer fewer, stronger edits:

- remove repetition, summary tone, and restated conclusions;
- replace puffery with the actual behavior or bound;
- remove empty introductory and participial phrases;
- replace vague abstractions with the component, action, or artifact;
- keep a flagged word when it is the precise term in context;
- do not use a banned-word list as mechanical find-and-replace.

Avoid:

- puffery such as pivotal, crucial, vital, groundbreaking, robust, or seamless;
- generic AI vocabulary such as delve, leverage, multifaceted, foster, realm, or tapestry;
- empty phrases such as "ensuring reliability" when no mechanism or test follows;
- repeated section summaries that add no decision or evidence;
- "In conclusion" or similar closing formulas in a design document;
- emoji decoration, excessive bold, em dash characters, and en dash characters.

Use punctuation that exposes the sentence structure: periods, commas, colons, semicolons, parentheses, and normal hyphens.

## 6. Format for scanning

- Use headings that tell the reader what the section owns.
- Use a paragraph for one connected idea.
- Use bullets for three or more parallel items.
- Use a table for exact mappings, fields, owners, alternatives, risks, and tests.
- Use numbered steps only when order matters.
- Use code formatting for literal identifiers, values, files, subjects, keys, and commands.
- Use bold sparingly to mark a term or decision, not every important word.
- Do not turn two clear sentences into a list.

Scannability must not fragment the design. Combine closely related facts when separating them would hide their dependency.

## 7. Distinguish facts, proposals, and requirements

| Kind         | Writing rule                                                                      |
| ------------ | --------------------------------------------------------------------------------- |
| Current fact | State the observed behavior and cite its authority.                               |
| Proposal     | Name it as proposed and state the acceptance or migration gate.                   |
| Requirement  | Use normative language only when an actor, behavior, and enforcement point exist. |
| Risk         | State the failure, impact, mitigation, and proof.                                 |
| Assumption   | Label it and state what invalidates it.                                           |

Do not use "should" when the document means a binding requirement. Do not use `MUST` or `SHALL` for a preference. Follow the repository's live normative-language rules.

## 8. Apply focused rewrites

| Before                                                                                     | After                                                                                                    | Why                                             |
| ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| It is important to note that dbt merges deletes into silver.                                 | dbt merges deletes into silver.                                                                        | Removes filler and leads with the actor.        |
| The schema is validated by the registry.                                                     | The registry validates the schema.                                                                     | Uses active voice.                              |
| The system provides a robust and seamless retry mechanism.                                   | The producer retries with idempotence on until the broker acknowledges or the delivery timeout expires. | Replaces promotion with observable behavior.    |
| The new approach enables improved reliability by ensuring invalid updates are not applied.   | A gold candidate that fails reconciliation is never swapped in, and the current gold stays published.  | Names the trigger, actor, behavior, and signal. |
| Various identifiers are used throughout the pipeline.                                        | The contract defines one natural key per source entity and one surrogate key per dimension version.    | Replaces a vague claim with exact concepts.     |

## 9. Run the copyedit pass

Review in this order:

1. Meaning: confirm no edit changed a decision, bound, owner, or contract.
2. Audience: confirm the reader can act without unexplained internal context.
3. Information order: move the decision before its background.
4. Paragraphs: keep one topic and one clear opening claim per paragraph.
5. Sentences: fix passive voice, vague actors, loose modifiers, and needless words.
6. Terminology: use one term per concept and preserve necessary distinctions.
7. Tone: remove puffery, defensiveness, casual filler, and AI-like summary prose.
8. Formatting: reduce excessive lists, bold, notes, and decorative headings.
9. Punctuation: remove em dash and en dash characters and check sentence boundaries.

Stop when the prose is clear and accurate. Do not rewrite correct sentences merely to make the edit look larger.
