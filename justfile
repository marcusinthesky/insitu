set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments

mod agents '.agents/justfile'

python := "env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV"

default:
    @just --list

# Prepare locked dependencies and Git hooks. This is the network-enabled setup step.
setup:
    test -s Cargo.lock
    {{ python }} uv sync --frozen
    cargo fetch --locked
    just agents::apply
    prek install

# Check prepared inputs without changing either lockfile.
dependencies:
    test -s Cargo.lock
    cargo metadata --format-version 1 --frozen > /dev/null
    {{ python }} uv lock --check --offline

# Replay the saved upstream revision in isolation (fetches the pinned template).
template-check:
    {{ python }} uv run --offline --frozen python scripts/template.py check

# Preview a guarded update to an explicit upstream commit without changing source.
template-preview revision:
    {{ python }} uv run --offline --frozen python scripts/template.py preview "$1"

# Apply a trialed update from a clean branch; review the resulting diff.
template-update revision:
    {{ python }} uv run --offline --frozen python scripts/template.py update "$1"

# Check tracked and untracked source without repairing files or fetching dependencies.
check:
    bash infra/nix/check.sh
    just agents::check

# Formatting is an explicit write, separate from check.
fmt:
    {{ python }} python3 scripts/format_text.py
    cargo fmt --all
    nixfmt devenv.nix flake.nix nix/*.nix infra/nix/pkgs/*.nix
    tombi format --offline
    rumdl fmt .
    ruff format
    while IFS= read -r -d '' f; do if [[ -f "$f" && "${f##*/}" == justfile ]]; then just --justfile "$f" --fmt --unstable; fi; done < <(git ls-files --cached --others --exclude-standard -z)

# Refresh this repository's generated documentation with the engine being developed.
sync:
    cargo run --quiet --frozen -p insitu -- sync

# Refresh advisory data and check dependency policy online.
security:
    cargo deny --locked check

# External links are a separate network check.
links:
    lychee --config lychee.toml .

# Advisory grammar pass over tracked Markdown. Spelling belongs to codespell, and
# title case and serial commas are not house style; the rest may be false positives.
prose:
    git ls-files -z '*.md' | xargs -0 harper-cli lint --no-color --ignore SpellCheck,UseTitleCase,OxfordComma

lint:
    cargo clippy --workspace --all-targets --frozen -- -D warnings

test:
    cargo nextest run --workspace --frozen
    cargo test --workspace --doc --frozen

docs:
    RUSTDOCFLAGS="${RUSTDOCFLAGS:-} -D warnings" cargo doc --workspace --no-deps --frozen

# Python SDK, packaging and npm launcher tests; these do not exercise the Rust engine.
test-portable:
    {{ python }} uv run --offline --frozen pytest -q
    node --test tests/node/launcher.test.cjs

# Documentation site in site/. Zensical copies all of docs_dir (the repository root)
# and has no exclude option, so it builds from an export of tracked and unignored files.
site:
    #!/usr/bin/env bash
    set -euo pipefail
    src=$(mktemp -d)
    trap 'rm -rf "$src"' EXIT
    git ls-files -z --cached --others --exclude-standard | tar --null --ignore-failed-read -T - -cf - | tar -xf - -C "$src"
    {{ python }} uv run --offline --frozen zensical build --strict --config-file "$src/mkdocs.yml"
    rm -rf site
    mv "$src/site" site

# Public API compatibility of the library crates against the last published release.
semver:
    cargo semver-checks --workspace

build:
    cargo build --release --frozen -p insitu

# Read-only parity run on a real project, e.g. `just parity ~/Git/pricing-perspective`.
# Extensions run under INSITU_PYTHON, so point it at that project's virtualenv.
parity project python=(project / "src/python/.venv/bin/python"): build
    INSITU_PYTHON="$2" target/release/insitu check --root "$1"

wheel:
    maturin build --release --locked --out dist/wheels

# `path:` reads the working tree, so untracked files are visible to the flake.
nix:
    nix flake check --no-update-lock-file path:.
    nix build --no-link --print-out-paths path:.
