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
    uv run ruff format
    uv run ruff check --fix

# Run every hook on every file (CI sets SKIP=gitleaks and runs secrets-scan instead)
lint:
    uv run prek run --all-files

# Create the next ADR from docs/tooling/adr-template.md: just adr-new "Title" [owner]
[positional-arguments]
adr-new title owner="platform":
    uv run python scripts/new_adr.py "$1" --owner "$2"
