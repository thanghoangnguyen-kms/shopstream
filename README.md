# Shopstream

[![ci](https://github.com/thanghoangnguyen-kms/shopstream/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/thanghoangnguyen-kms/shopstream/actions/workflows/ci.yml)

An enterprise-grade open lakehouse that runs on a 16 GB laptop, built in public over 23 weeks. Synthetic, deliberately messy e-commerce data will flow from Postgres CDC and a clickstream generator through Kafka into Apache Iceberg. dbt will model it into a Kimball gold layer, served through a semantic layer to an agent behind a guarded MCP server.

The point is **proof, not components**. Every week ends with a test, CI gate or `just` command that turns a claim into evidence.

> **Status: Week 1 of 23 (repo foundations).** Only the foundations below exist so far; the roadmap says when the rest lands.

## Quick start

You need macOS or Linux (Windows: use WSL) and [uv](https://docs.astral.sh/uv/) 0.12.18 or newer.

```bash
git clone https://github.com/thanghoangnguyen-kms/shopstream.git
cd shopstream
uv sync && uv run just setup
uv run just check
```

## What's here now

- **One lockfile for every tool:** a uv workspace on Python 3.13, with ruff, sqlfluff, mypy, zizmor, pytest, prek and just pinned in `uv.lock`.
- **The same hooks locally and in CI:** prek runs ruff, sqlfluff, `mypy --strict`, zizmor (GitHub Actions security), gitleaks and file hygiene.
- **Four required checks:** `lint`, `test`, `secrets` (full-history gitleaks) and `pr-title` (Conventional Commits). Every action is pinned to a full commit SHA.
- **Gates that are proven to fail:** tests plant a fake secret, break the docs conventions on purpose and feed bad PR titles, and each one must go red.
- **Renovate** keeps pins current, with a 3-day minimum release age.
- **A documentation system** with enforced frontmatter, owner roles and MADR decision records: start at [docs/README.md](docs/README.md) and [ADR-000](docs/adr/adr-000-record-architecture-decisions.md).

## Roadmap

| Checkpoint | Week | What it proves |
| ---------- | ---- | -------------- |
| v0.4       | 12   | A batch lakehouse: CDC → bronze → silver → Kimball gold, contracts as CI gates, governance, blue/green publish |
| v0.6       | 17   | Tuned and operable: Kafka and Spark benchmarks, Airflow assets, SLOs, a deterministic nightly rebuild |
| v1.0       | 23   | Privacy, resilience and AI: GDPR erasure with byte-level proof, a DR drill, a semantic layer behind a guarded MCP server, a text-to-SQL eval |

## Owners

Docs, and dbt groups from Week 12, name one of three owner roles: `platform`, `analytics-eng` or `governance`. This is a solo project and one person plays all three, so the owner field shows where each responsibility would sit in a real team.

## License

[Apache-2.0](LICENSE)
