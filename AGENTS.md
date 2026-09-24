# AGENTS.md: rules for coding agents (and humans) in this repo

Shopstream is a public, 23-week data engineering capstone: an open lakehouse on a laptop, with evidence-backed enterprise practices. **Agent-written changes meet exactly the same gates as human changes.**

## 1. Gates

- Run `just check` before proposing a change. It runs the same hooks, tests and secret scan as CI.
- Never bypass hooks (`--no-verify`, `SKIP=`). The one exception is the documented Week 1 planted-secret proof.
- Never hand-edit generated files: `uv.lock` (use `uv add` / `uv lock`), anything under `.tools/`, dbt `target/`.
- Commit messages and PR titles follow Conventional Commits. PRs are squash-merged.
- A new dependency, service or tool needs an ADR first: `just adr-new "<title>" [owner]`.

## 2. Repo map

| Path                   | What it holds                                                              |
| ---------------------- | -------------------------------------------------------------------------- |
| `packages/<member>/`   | uv workspace members (Python 3.13)                                         |
| `scripts/`             | tested helper scripts behind `just` recipes                                |
| `tests/`               | repo-level gates: secrets, docs lint, workspace naming, PR titles, policy  |
| `docs/`                | specs, ADRs, schema and evidence (see §3)                                  |
| `.github/workflows/`   | the four required checks: `lint`, `test`, `secrets`, `pr-title`            |
| `.claude/skills/`      | the documentation skills (§5)                                              |

**Naming:** each workspace member `packages/<member>/` is the distribution `shopstream-<member>`, imported as `shopstream_<member>`. `tests/test_workspace.py` enforces this.

## 3. Docs Layout

Where each documentation domain lives, and the status lifecycle: [docs/claude/docs-layout.md](docs/claude/docs-layout.md).

## 4. Spec Authoring

The normative rules for anything under `docs/specs/` or `docs/adr/`: [docs/claude/spec-authoring.md](docs/claude/spec-authoring.md). `tests/test_docs_integrity.py` enforces them. Decisions start at [ADR-000](docs/adr/adr-000-record-architecture-decisions.md).

## 5. Skills

Invoke these for documentation work; each one lives in `.claude/skills/<name>/SKILL.md`. `tests/test_skills.py` fails if this list and the skills folder disagree.

| Skill                | Use it to                                                                                              |
| -------------------- | ------------------------------------------------------------------------------------------------------ |
| `/shop-spec`         | author a new PRD, TRD, ADR, REF, GUIDE or GLOSSARY (8 phases, including the gotcha audit and the review gate) |
| `/shop-write-doc`    | rewrite, review or update an existing doc, evidence first                                              |
| `/shop-housekeeping` | audit and repair the docs tree: lint, banned terms, drift, duplication                                 |

## 6. Platform gotchas

These facts were verified on 2026-09-24. A document or change must not contradict them, and `/shop-spec` Phase 5 cites them by ID. Re-verify any row older than about three months against a primary source before relying on it.

| ID  | Gotcha |
| --- | ------ |
| G1  | DuckDB, including dbt 2.0's built-in DuckDB, writes Iceberg through the REST catalog. New tables are **v2** unless `format-version=3` is set, and only merge-on-read is supported. |
| G2  | DuckDB has **no Iceberg branches and no remote signing**, so write-audit-publish is impossible on DuckDB-written tables. |
| G3  | The Iceberg Kafka Connect sink is **append-only, v2, exactly-once**, with no upsert mode (its v3 writer, PR #14797, is unmerged). |
| G4  | Only Spark 4.1 + Iceberg 1.11 and DuckDB 1.5.3+ write Iceberg v3. PyIceberg, Polars and ClickHouse only read v3. |
| G5  | SeaweedFS replaces MinIO, whose community images were pulled. Keep bucket versioning and object-lock **off** (conditional-write bug #8073). |
| G6  | Lakekeeper's OpenFGA authorization only works with authentication on (OIDC or Kubernetes). |
| G7  | dbt-mcp's semantic-layer tools need a dbt Platform (Cloud) account, so Shopstream uses a custom MCP server. |
| G8  | Spark Real-Time Mode runs on classic compute only, **not** on Databricks Free Edition (serverless). |
| G9  | Kafka share groups (KIP-932) are production-ready in Kafka 4.2, but the Python client (librdkafka) is preview-only. |
| G10 | Deletion vectors **mask** rows; they don't erase them. Erasure needs rewrite → `expire_snapshots` → `remove_orphan_files`. |
| G11 | `sqlfluff-templater-dbt` needs `dbt-core` 1.x; dbt 2.0 (the `dbt` package on PyPI) doesn't provide it. |
| G12 | The official `spark:4.1.x` / `4.2.0` Python images ship Python 3.10, and PySpark refuses a driver/worker minor-version mismatch with this repo's 3.13. |
