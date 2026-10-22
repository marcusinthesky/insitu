set shell := ["bash", "-euc"]

default:
    @just --list

# Every gate in .pre-commit-config.yaml; includes untracked files as well as tracked ones.
check:
    git ls-files --cached --others --exclude-standard -z | xargs -0 -r prek run --files

fmt:
    cargo fmt --all
    nixfmt devenv.nix flake.nix nix/*.nix
    tombi format --offline
    rumdl fmt .
    ruff format
    for f in $(git ls-files --cached --others --exclude-standard | grep -E '(^|/)justfile$'); do just --justfile "$f" --fmt --unstable; done

# Advisory grammar pass over tracked Markdown. Spelling belongs to codespell, and
# title case and serial commas are not house style; the rest may be false positives.
prose:
    git ls-files -z '*.md' | xargs -0 harper-cli lint --no-color --ignore SpellCheck,UseTitleCase,OxfordComma

lint:
    cargo clippy --workspace --all-targets --locked -- -D warnings

test:
    cargo nextest run --workspace --locked
    cargo test --workspace --doc --locked

docs:
    RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps --locked

# Python SDK, packaging and npm launcher tests; these do not exercise the Rust engine.
test-portable:
    uv run --frozen pytest -q
    node --test tests/node/launcher.test.cjs

# Documentation site in site/. Zensical copies all of docs_dir (the repository root)
# and has no exclude option, so it builds from an export of tracked and unignored files.
site:
    #!/usr/bin/env bash
    set -euo pipefail
    src=$(mktemp -d)
    trap 'rm -rf "$src"' EXIT
    git ls-files -z --cached --others --exclude-standard | tar --null --ignore-failed-read -T - -cf - | tar -xf - -C "$src"
    uv run --frozen zensical build --strict --config-file "$src/mkdocs.yml"
    rm -rf site
    mv "$src/site" site

# Public API compatibility of the library crates against the last published release.
semver:
    cargo semver-checks --workspace

build:
    cargo build --release --locked -p insitu

# Read-only parity run on a real project, e.g. `just parity ~/Git/pricing-perspective`.
# Extensions run under INSITU_PYTHON, so point it at that project's virtualenv.
parity project python=(project / "src/python/.venv/bin/python"): build
    INSITU_PYTHON={{ python }} target/release/insitu check --root {{ project }}

wheel:
    maturin build --release --locked --out dist/wheels

# `path:` reads the working tree, so untracked files are visible to the flake.
nix:
    nix flake check --no-update-lock-file path:.
    nix build --no-link --print-out-paths path:.
