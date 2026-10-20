# Insitu

**Status: alpha.** A native Rust engine for reactive Markdown and front-matter
materializations, replacing the Python engine at
`7f3a1b8327a3e14ac9ac2d8a4b25b19fcdab8daa`. Existing `insitu.toml`, managed
comment markers, protocol v1 and the Python extension SDK are the compatibility
target; known differences are in [docs/compatibility.md](docs/compatibility.md).

## Usage

```sh
insitu init
insitu sync
insitu check
insitu watch
insitu extension add ./my-extension
insitu extension list
```

`check` uses an in-memory index and does not commit patches: 0 means current,
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

## Repository

| Component | Responsibility |
| --- | --- |
| `crates/insitu` | CLI and filesystem watch loop |
| `crates/insitu-core` | Scanning, SQLite index, SQL/Jinja, patches, Git, extension installation |
| `crates/insitu-protocol` | Validated v1 models and bidirectional framed JSON-RPC |
| `crates/insitu-sdk` | Typed Rust extension registration |
| `python/insitu` | Python extension SDK; no Python engine |
| `packaging/npm` | No-download launcher for optional native platform packages |
| `flake.nix` | Nix package and app |

Start reading at `Project::run` in `crates/insitu-core/src/project.rs`, then
`document.rs` and `patch.rs`. The [architecture guide](docs/architecture.md)
explains ownership and invariants; [protocol](docs/protocol.md) describes the
extension wire format.

## Development

[devenv](https://devenv.sh) pins every tool through `devenv.lock`. Install the
pinned CLI with `bash nix/install-devenv.sh`, then run `direnv allow` or
`devenv shell`. Entering the shell syncs the Python SDK's dependencies from
`uv.lock` and installs the prek Git hooks.

```sh
just check        # every gate in .pre-commit-config.yaml, as the hooks run them
just test         # nextest and doctests
just lint         # Clippy, warnings denied
just parity ~/Git/pricing-perspective   # read-only `check` on a real project
just fmt          # format Rust, Nix, TOML, Markdown, Python and justfiles
```

`rust-toolchain.toml` is the minimum supported Rust version, which CI tests on
Linux, macOS and Windows; the devenv shell uses a newer pinned toolchain.
Installation channels (Cargo, PyPI, npm, Nix) are described in
[docs/distribution.md](docs/distribution.md).
