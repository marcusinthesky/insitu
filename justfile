set shell := ["bash", "-uc"]

default:
    @just --list

# Install the locked runtime and development dependencies.
setup:
    uv sync --locked --all-groups

_pytest markexpr *args:
    #!/usr/bin/env bash
    set -euo pipefail
    cd '{{ source_directory() }}'
    exec uv run --locked pytest -q -p no:monitor -n auto -m '{{ markexpr }}' {{ args }}

# Run the maintained unit and integration tests.
test *args: (_pytest "not benchmark" "--testmon-forceselect" args)

# Run all tests, including opt-in benchmarks.
test-all *args: (_pytest "" args)

# Run package linting.
lint:
    uv run --locked ruff check .
    uv run --locked deptry .

# Run strict static type checking.
typecheck:
    uv run --locked pyrefly check src tests

# Format Python and TOML sources.
format:
    uv run --locked ruff check --fix .
    uv run --locked ruff format .
    tombi format --offline pyproject.toml

# Verify formatting without changing files.
format-check:
    uv run --locked ruff format --check .
    tombi format --offline --check pyproject.toml

# Run every prek quality gate.
check:
    prek run --all-files

# Build and validate the documentation site.
docs-check:
    uv run --locked zensical build --strict

# Preview the documentation site locally.
docs-serve:
    uv run --locked zensical serve

# Exercise the documented example in an isolated temporary project.
examples-check:
    bash scripts/check-quickstart.sh

# Continuously rerun tests through devenv's process manager.
watch:
    devenv up
