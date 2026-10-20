# Insitu — Rust rewrite candidate

**Status: compiles and passes its native, conformance and parity gates; not yet a published release.**

This repository targets the behavior of `marcusinthesky/insitu` at commit
`7f3a1b8327a3e14ac9ac2d8a4b25b19fcdab8daa`. It contains a native host, v1
process protocol, Rust and Python extension SDKs, tests, and packaging configuration.
The engine is Rust, not a wrapper that falls back to the old Python engine.

What has been verified, on Linux x86-64:

- The workspace builds, and Clippy, rustdoc and `cargo-deny` pass with warnings denied.
- `scripts/conformance.py` passes byte-for-byte against the Python reference.
- On a real repository (`pricing-perspective`, 112 managed regions), `insitu check`
  reports nothing stale against content the Python engine wrote, flags the same
  tampered region, and `sync` restores it byte-for-byte.

Not yet verified: macOS and Windows builds, wheels, npm packages and publication.
Known differences, including the Python engine internals this rewrite does not
provide, are in [docs/compatibility.md](docs/compatibility.md).
[verification/](verification/) is the original authoring record, from before any of this ran.

## What is in the repository

| Component | Responsibility |
| --- | --- |
| `crates/insitu` | CLI and filesystem watch loop |
| `crates/insitu-core` | Scanning, SQLite index, SQL/Jinja, patches, Git, extension installation |
| `crates/insitu-protocol` | Validated v1 models and bidirectional framed JSON-RPC |
| `crates/insitu-sdk` | Typed Rust extension registration |
| `python/insitu` | Compatible decorator-oriented Python SDK, no Python engine |
| `packaging/npm` | No-download launcher for optional native platform packages |
| `flake.nix` | Nix package, app and development shell |
| `scripts` | Bootstrap, tests, conformance, staging and gated publication |

## Development environment

[devenv](https://devenv.sh) pins every tool (Rust toolchain with rust-analyzer,
cargo-nextest, cargo-deny, Python, uv, Node, and the repository linters) through
`devenv.lock`. Install the pinned CLI with `bash nix/install-devenv.sh`, then run
`direnv allow` or `devenv shell`. Entering the shell syncs the Python SDK's
dependencies from `uv.lock` and installs the prek Git hooks.

```sh
just check        # every gate in .pre-commit-config.yaml, as the hooks run them
just test         # nextest and doctests
just lint         # Clippy, warnings denied
just conformance  # black-box CLI checks against a release build
just parity ~/Git/pricing-perspective   # read-only `check` on a real project
just fmt          # format Rust, Nix, TOML, Markdown, Python and justfiles
```

`rust-toolchain.toml` is the minimum supported Rust version. CI tests it with
rustup on Linux, macOS and Windows; the devenv shell uses a newer pinned toolchain.

## User interface targeted by the rewrite

```sh
insitu init
insitu sync
insitu check
insitu watch
insitu extension add ./my-extension
insitu extension list
```

Existing `insitu.toml`, managed comment markers and protocol v1 are the compatibility
target. `check` uses an in-memory index and does not commit patches: 0 means current,
1 means stale, and 2 means an error. `--json` is an additive reporting option.

```markdown
<!-- insitu:begin include
id = "example"
path = "snippet.txt"
language = "text"
-->
<!-- insitu:end -->
```

Generated bodies and generated front matter are excluded from the default SQL
source projection. Source writes are staged, checked for stale snapshots and
committed by per-file atomic replacement. This is **not** a crash-atomic transaction
across multiple files. Extensions are trusted OS processes, not sandboxes.

## Distribution design

The proposed release uses the same native CLI through all channels:

| Channel | Build/package route | Status in this archive |
| --- | --- | --- |
| Cargo / crates.io | Four Cargo packages; CLI installs as `insitu` | Builds and tests on Linux; not published |
| PyPI | Maturin `bin` wheels plus `insitu.sdk` Python package | Configuration and tested SDK; wheels not built |
| npm | Scoped wrapper plus five optional native packages | Launcher tested; native packages not built |
| Nix | Flake package/app, buildRustPackage | Locked; `nix build` verified on Linux |

Names and registry ownership have not been verified. npm's proposed scope is
`@marcusinthesky`. Nix flake availability is not the same as inclusion in nixpkgs;
that requires a separate reviewed submission. See [distribution](docs/distribution.md).

## Read the code in this order

Start with `Project::run` in `crates/insitu-core/src/project.rs`, then `document.rs`
and `patch.rs`. They explain the flow without an actor framework or dependency
injection layer. Process concurrency lives in `insitu-protocol`; boxed futures
are confined to recursive RPC dispatch and the extension registration boundary.
The [architecture guide](docs/architecture.md) explains ownership and invariants.

## Validation

`just test-portable` exercises the Python SDK and Node launcher; it **does not
validate Rust**. `just test` runs the native unit, protocol and integration tests.
`scripts/conformance.py` additionally requires a real binary and can compare results
with the pinned Python source:

```sh
python scripts/conformance.py \
  --binary target/release/insitu \
  --oracle-python /absolute/path/to/reference-venv/bin/python \
  --oracle-source /absolute/path/to/reference-insitu/src
```

The reference environment must have the original project's dependencies installed.
This comparison is intentionally byte-for-byte, including generated YAML formatting;
a difference is a failure, not an automatically accepted migration.

[Compatibility](docs/compatibility.md) · [Protocol](docs/protocol.md) ·
[Release procedure](docs/distribution.md) · [Security](SECURITY.md)
