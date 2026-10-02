set shell := ["bash", "-euo", "pipefail", "-c"]

# Renovate bumps this (customManagers in renovate.json).
gitleaks_version := "8.30.1"

# List the recipes
default:
    @just --list

# Install the pinned gitleaks into .tools/bin, checksum-verified
tools:
    uv run python scripts/install_gitleaks.py {{gitleaks_version}}

# Run the test suite (installs gitleaks first: test_secret_gate needs it)
test: tools
    uv run pytest

# One-time setup after cloning: sync the venv, install gitleaks, install the git hooks
setup:
    uv sync --locked
    {{just_executable()}} tools
    uv run prek install

# Format Python and apply safe lint fixes
fmt:
    uv run ruff check --fix
    uv run ruff format

# Run every hook on every file (CI sets SKIP=gitleaks and runs secrets-scan instead)
lint:
    uv run prek run --all-files

# Create the next ADR from docs/tooling/adr-template.md: just adr-new "Title" [owner]
[positional-arguments]
adr-new title owner="platform":
    uv run python scripts/new_adr.py "$1" --owner "$2"

# Scan the full git history for secrets (what the CI `secrets` job runs)
secrets-scan: tools
    .tools/bin/gitleaks git --config .gitleaks.toml --redact --no-banner --verbose .

# Bring the core stack up from a clean clone, refusing a Colima VM under 12 GiB
up:
    uv run python scripts/stack.py up

# Stop every profile's containers; pass --volumes to delete the named volumes
[positional-arguments]
down *args:
    uv run python scripts/stack.py down "$@"

# Sample docker stats every 5 s into .mem/samples.jsonl until Ctrl-C or --duration SECONDS
[positional-arguments]
mem-sample *args:
    uv run python scripts/mem_report.py sample "$@"

# Report peak memory, mem_limit totals and each container's OOM, exit-code and state check; non-zero on a breach (--min-frames N)
[positional-arguments]
mem-report *args:
    uv run python scripts/mem_report.py report "$@"

# Build and document the dbt project on the offline ci target with no dbt login: what CI's test job runs
dbt-ci:
    uv sync --locked --project analytics/dbt
    DO_NOT_TRACK=1 uv run --frozen --project analytics/dbt dbt build --target ci --project-dir analytics/dbt --profiles-dir analytics/dbt
    DO_NOT_TRACK=1 uv run --frozen --project analytics/dbt dbt docs generate --target ci --static --project-dir analytics/dbt --profiles-dir analytics/dbt

# Everything CI runs, in one command: a local pass predicts a CI pass
check: tools lint test dbt-ci secrets-scan
