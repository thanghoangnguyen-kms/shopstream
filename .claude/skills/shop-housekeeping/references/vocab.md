# Vocab Map

Shopstream's banned-term map. Add a row when a decision retires a name. `shop-housekeeping` replaces every occurrence under `docs/`, and scan.md §BLOCK gets one grep per row. Entries accumulate as decisions land.

## Banned Terms → Replacements

| Find    | Replace with | Notes                                                                                                                   |
| ------- | ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `MinIO` | `SeaweedFS`  | MinIO's community images were pulled (removed from Docker Hub 2026-09-11); `AGENTS.md` G5. Exception: a sentence that explains the replacement |

---

## SSOT Pointer Table

Replace duplicated content in non-SSOT docs with a pointer.

| Content type                          | SSOT                                         | Pointer format            |
| ------------------------------------- | -------------------------------------------- | ------------------------- |
| Writer/reader split (sole writers)    | `docs/specs/platform/ref-architecture.md`    | `ref-architecture.md §6`  |
| Platform invariants                   | `docs/specs/platform/ref-architecture.md`    | `ref-architecture.md §8`  |
| Frontmatter fields, owners, statuses  | `docs/tooling/frontmatter-schema.yaml`       | `frontmatter-schema.yaml` |
| Platform gotchas                      | `AGENTS.md` §6                               | `AGENTS.md G<N>`          |
