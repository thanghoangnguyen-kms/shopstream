# Review and Validation

Read this reference for the final review and fix-and-retry loop.

## Contents

1. Review in independent passes
2. Build an acceptance matrix
3. Discover live commands
4. Validate examples
5. Run the fix loop
6. Final handoff

## 1. Review in independent passes

| Pass             | Questions                                                                                                                                                                          |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Structure        | Does the document use the current type shape, lifecycle, length, and frontmatter?                                                                                                  |
| Authority        | Does every important claim agree with accepted decisions, canonical REFs, schemas, code, and tests?                                                                                |
| Contracts        | Are names, types, patterns, examples, ownership, lifecycle, and compatibility consistent?                                                                                          |
| Failure behavior | Are rejection, retry, rollback, deletion, degraded state, security, and safety boundaries explicit?                                                                                |
| Testability      | Can each important requirement be falsified by a scenario and observable result?                                                                                                   |
| Writing          | Does the prose fit its audience, lead with the main point, preserve meaning, use direct and concrete language, and avoid feedback residue, needless repetition, or AI-like filler? |
| Diagrams         | Do diagrams add understanding, follow the live style guide, render, and use canonical terms?                                                                                       |
| Change safety    | Did the work stay inside the authorized files and preserve unrelated changes?                                                                                                      |

Do not combine these into one impressionistic read. Separate passes catch different failures.

## 2. Build an acceptance matrix

Map every requested outcome to proof:

| Outcome                    | Artifact evidence                         | Validation evidence                                       | Status       |
| -------------------------- | ----------------------------------------- | --------------------------------------------------------- | ------------ |
| Canonical naming           | Contract table and examples use one term  | Retired-term scan returns no matches                      | pass or fail |
| Valid examples             | Fenced JSON matches the documented schema | Every extracted JSON block passes `jq`                    | pass or fail |
| Understandable integration | Focused overview and sequence diagrams    | Mermaid renders and visual review passes                  | pass or fail |
| Bounded rewrite            | Only the target file changed              | Before and after `git status --short` agree outside scope | pass or fail |

Completion requires evidence for the original outcome, not just a clean diff.

## 3. Discover live commands

Read current repository scripts and testing guidance. For Shopstream documentation-only changes, the default gate is:

```bash
uv run pytest tests/test_docs_integrity.py -q
uv run prek run --all-files
git diff --check
git status --short
```

Add focused checks required by the target document:

- parse every JSON example with `jq`;
- validate YAML or other schemas with the repository tool that owns them;
- resolve relative links and frontmatter through the current docs checks;
- preview changed Mermaid in the PR's rendered file view (GitHub renders Mermaid);
- compare the line count with the live style-guide cap;
- scan for retired terms, inconsistent aliases, placeholder text, and forbidden punctuation;
- inspect the final diff for accidental edits and whitespace damage.

Useful focused scans:

```bash
wc -l <document>
rg -n 'TO[D]O|TB[D]|FIXME|REVIEW|COMMENT' <document>
rg -n '\x{2013}|\x{2014}' <document>
rg -n '<retired-name-1>|<retired-name-2>' <document>
```

Review matches with judgment. A word in a test case or a quoted migration rule may be intentional.

## 4. Validate examples

Treat examples as executable contract evidence:

- JSON must parse and use the same field names and enum case as the tables.
- YAML must parse and match the current schema when a validator exists.
- Subjects, keys, URLs, and environment examples must distinguish patterns from concrete values.
- UUIDs, timestamps, and IDs must use the documented representation.
- One representative example must retain every normative schema shape after compression.

If an example cannot be validated automatically, state the manual check performed.

## 5. Run the fix loop

For each failed check:

1. capture the exact failure;
2. identify whether the defect is in the document or pre-existing repository state;
3. fix only the in-scope defect;
4. rerun the focused check;
5. rerun any broader check affected by the fix.

Stop and request direction if a fix requires unauthorized files, changes an accepted decision, or expands the design materially.

Do not weaken a requirement, remove a test, or hide a warning to make validation green.

## 6. Final handoff

Report:

- the changed files;
- the main design and contract outcomes;
- commands that passed;
- checks not run and why;
- pre-existing failures with exact evidence;
- remaining decisions or implementation prerequisites.

Do not report internal planning notes, review residue, or a long edit diary.
