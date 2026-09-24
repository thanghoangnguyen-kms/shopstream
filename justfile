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
