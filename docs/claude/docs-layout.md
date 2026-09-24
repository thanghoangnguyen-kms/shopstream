# Docs Layout

Agent-facing map of the documentation domains and the document status lifecycle. It's linked from `AGENTS.md`.

Canonical documentation lives in `docs/`. Operational READMEs and agent instructions stay beside their consumers.

**The filesystem is the inventory.** This page names domains, never individual files; a hand-maintained tree rots against the tree it describes. List a directory to see what's in it.

## Domains under `docs/`

| Path          | Owns                                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------ |
| `specs/`      | PRDs (what), TRDs (how), REFs (SSOT), GUIDEs and GLOSSARYs, grouped by domain folder; taxonomy in `specs/README.md` |
| `adr/`        | Architecture Decision Records, `adr-NNN-<topic>.md`; numbers unique repository-wide                    |
| `claude/`     | Agent-facing pages linked from `AGENTS.md`                                                             |
| `tooling/`    | `frontmatter-schema.yaml` (the field schema the lint reads) and `adr-template.md`                      |
| `evidence/`   | Prove-step evidence (hook output, CI run links, measured results). Kept permanently. Created in W1 PR #4 |
| `benchmarks/` | Kafka (W7) and Spark (W14) benchmark reports. Kept permanently                                         |
| `eval/`       | Text-to-SQL evaluation reports (W22). Kept permanently                                                 |

Docs-root files: `README.md` (docs home), `CONTRIBUTING.md`, `documentation-map.md`.

## Frontmatter

Every file in `docs/specs/` and `docs/adr/` opens with a YAML frontmatter block containing at least `title`, `type`, `status`, `owner` (`platform` | `analytics-eng` | `governance`).

`docs/CONTRIBUTING.md` owns the typed relationship keys, the required sections per type, and the naming rules. `docs/tooling/frontmatter-schema.yaml` is the authoritative schema behind both, and `tests/test_docs_integrity.py` enforces it. Update `docs/documentation-map.md` only when introducing a canonical domain.

## Document status lifecycle

| Status     | Meaning                                                    |
| ---------- | ---------------------------------------------------------- |
| Draft      | Work in progress; not ready for review                     |
| Proposed   | Ready for review; no decision yet                          |
| Accepted   | Decision made; authoritative for implementation            |
| Active     | In use (REF, GUIDE, GLOSSARY)                              |
| Rejected   | Considered and declined; kept so the reasoning stays       |
| Deprecated | Sunset; kept for reference only                            |
| Superseded | Replaced by a specific successor; git preserves history    |
