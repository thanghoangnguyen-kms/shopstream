# Scan Checklist

Run all checks in parallel. Emit findings; no fixes here.

SSOTs: [vocab.md](vocab.md) · [`docs/tooling/frontmatter-schema.yaml`](../../../../docs/tooling/frontmatter-schema.yaml) · [`docs/documentation-map.md`](../../../../docs/documentation-map.md)

---

## BLOCK

```bash
# Every mechanical convention: frontmatter, enums, ADR numbers, relationships,
# residue headings, relative links, filenames
uv run pytest tests/test_docs_integrity.py -q

# Banned terms: one grep per row of vocab.md "Banned Terms"
grep -rn --include='*.md' 'MinIO' docs/
```

---

## WARN

```bash
# Status drift: docs still Draft or Proposed although the week that implements them has shipped.
# Compare each status with the README roadmap and the merged PRs.
grep -rn '^status:' docs/specs/ docs/adr/

# SSOT duplication: docs that restate the writer/reader split instead of linking to it
grep -rln --include='*.md' -i 'sole writer' docs/specs/ | grep -v 'platform/ref-architecture.md'
```

---

## NOTE

```bash
# `updated:` older than the file's last commit
for f in $(git ls-files 'docs/specs/*.md' 'docs/adr/*.md'); do
  fm=$(sed -n 's/^updated: *//p' "$f" | head -1)
  last=$(git log -1 --format=%cs -- "$f")
  if [ -n "$fm" ] && [ "$fm" \< "$last" ]; then echo "NOTE $f updated: $fm < last commit $last"; fi
done
```

---

## Ref Integrity Rules

- Typed relationship values are relative paths from the doc's own location, as one path or a list.
- `decided-by:` points only at `docs/adr/*.md`.
- Never edit an ADR's Context, Decision Outcome or Consequences after `status: Accepted`; supersede it.

---

## Status Lifecycle

```
Draft → Proposed → Accepted → Active
              ↘ Rejected   ↘ Deprecated / Superseded
```

Frontmatter `status:` is authoritative.

Allowed transitions in a housekeeping pass:

- `Draft → Draft`: no gate
- `Deprecated → Accepted` (un-deprecate): only with the owner's approval, recorded in the PR description
- `Proposed → Accepted`: requires the shop-spec Phase 6 critique at ≥ 13/15

---

## Compression Rules (Phase 7)

Apply to every touched doc.

| Rule                     | Action                                                                                          |
| ------------------------ | ----------------------------------------------------------------------------------------------- |
| `§1 Objective` prose     | Replace it with the attribute table                                                             |
| Alternatives in prose    | Move them to Pros and Cons (ADRs) or an `\| Option \| Why not \|` table                          |
| Preamble phrases         | Strip "Note that…", "It should be noted…", "It is worth mentioning…", "As mentioned above…"     |
| 3+ parallel prose items  | Convert to a table or a bullet list                                                             |
| Body repeats frontmatter | Remove it from the body                                                                         |
| Single-item bold/heading | Flatten it to inline prose                                                                      |
| Redundant "See also"     | Remove it if the reference is already in the frontmatter                                        |
