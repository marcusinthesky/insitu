# Architecture

How Insitu is built, for people and agents about to change it. The structure
follows the [ARCHITECTURE.md](https://architecture.md/) template, and this file is
itself an Insitu document: the project tree and the component table below are
generated regions, so they cannot drift from the repository.

## 1. Project structure

<!-- insitu:begin tree
id = "structure"
depth = 2
directories_only = true
-->

```text
insitu/
├── .github/
│   └── workflows/
├── crates/
│   ├── insitu/
│   ├── insitu-core/
│   ├── insitu-protocol/
│   └── insitu-sdk/
├── docs/
├── insitu/
│   ├── models/
│   └── templates/
├── nix/
├── packaging/
│   └── npm/
├── python/
│   └── insitu/
├── schemas/
├── scripts/
└── tests/
    ├── fixtures/
    ├── node/
    └── python/
```

<!-- insitu:end -->

`insitu/` and `insitu.toml` are this repository's own Insitu project, the models and
templates behind the generated regions here and in the README.

## 2. System overview

```text
Markdown + front matter ─scan─▶ SQLite index ─SQL models─▶ rows
        ▲                                                    │
        │                                       templates, extensions
        │                                                    ▼
        └──────────── checked, staged write ◀──── rendered regions
```

One pass reads every source, computes every region, and writes back only the files
whose content changed. `check` runs the same pass against an in-memory index and
reports instead of writing.

## 3. Core components

The table is generated from the front matter of each crate's README.

<!-- insitu:begin sql
id = "components"
model = "components"
template = "components.md.j2"
-->

| Crate | Responsibility | Start reading |
| --- | --- | --- |
| [insitu](crates/insitu/README.md) | The command line, its reports and the filesystem watch loop; it computes nothing itself. | [`src/main.rs`](crates/insitu/src/main.rs) |
| [insitu-core](crates/insitu-core/README.md) | The engine — scan, SQLite index, plan, render, then a checked, staged commit of every changed file. | [`src/project.rs`](crates/insitu-core/src/project.rs) |
| [insitu-protocol](crates/insitu-protocol/README.md) | Version 1 wire format for extensions — validated models and a bidirectional JSON-RPC peer over framed stdio. | [`src/peer.rs`](crates/insitu-protocol/src/peer.rs) |
| [insitu-sdk](crates/insitu-sdk/README.md) | Typed registration for Rust extensions — resources, transforms, renderers and validators as async handlers. | [`src/lib.rs`](crates/insitu-sdk/src/lib.rs) |

<!-- insitu:end -->

Outside the workspace, `python/insitu` is the Python extension SDK (there is no
Python engine), `packaging/npm` is a launcher for the native binaries, and
`flake.nix` packages the CLI for Nix.

### 3.1. One owner, concrete data

The CLI owns a `Project`. Project owns Config, Policy, Index, Builtins, Registry and
Process handles. Policy decides which paths are sources; Index is only the SQLite
projection. Pure computations remain synchronous. There is no shared mutable
engine behind an async mutex, no general actor framework and no generic repository
backend. Each invocation carries owned options, location and dependency trace.

`Project::run` scans/indexes, plans concrete jobs, validates capabilities, renders,
prepares a ChangeSet and commits only in Sync mode. A report is plain data. Check
uses the same calculations but reports stale files and never commits the ChangeSet.
Most operational errors use contextual `anyhow::Result`; RPC has a concrete error
enum. A small BTreeMap-based graph makes ordering deterministic.

### 3.2. Files and ownership

`Document` owns the source string and byte ranges. Regions are recognized outside
fenced code; output is replaced only between the opening and closing markers.
Templates are read as LF, and generated output takes the document's own line
ending (CRLF if its first line ends in CRLF, otherwise LF), so results do not
depend on how Git checked the files out.
`patch.rs` stages complete replacement files on the destination filesystem, checks
original bytes before writes and attempts rollback after a replacement failure.
YAML syntax trees exist only inside synchronous editing functions; they do not
cross Tokio task boundaries.

The engine separates authored source from generated output. SQL sees a projection
without generated Markdown bodies and without front-matter `insitu`/`generated`.
An ownership graph relates files, output targets and materializations; a self-include
creates a dependency cycle instead of repeatedly expanding the document.

### 3.3. Extension concurrency

Each process has a `Peer`. Its reader task routes numeric-ID responses independently
from inbound method calls. While Project awaits an extension result, it services
callbacks from an inbound channel. A callback can resolve a nested extension
resource without blocking the peer's response reader. The recursive calls back into
`invoke` are the only boxed futures.

The capability registry maps each capability to a `Handler`: a `Builtin` variant or
an extension's process index and capability name.
Transforms run base -> topologically ordered decorators -> contributors. Replacement
requires explicit project approval. Validators produce structured diagnostics.

### 3.4. Extension authoring

Python keeps the decorator API, Pydantic TypeAdapter validation, sync/async handler
support and Context.resource. Rust uses registration methods with generic parameter
and result types; Serde and Schemars handle the boundary. Domain code does not carry
those generic parameters. `insitu-sdk` re-exports the protocol types that handlers
use, so an extension depends on that crate alone; its rustdoc includes an example.

## 4. Data stores

The only store is a disposable SQLite index at `.insitu/index.db` (`check` keeps
it in memory). `_documents` holds one row per Markdown source: path parts, title,
body, front matter as JSON, hash and file stats. The `documents` view adds one
column per front-matter key, which is what SQL models query; `entries` lists every
file and directory for `tree`. Deleting the index loses nothing.

## 5. External integrations

- **Git.** Sources respect `.gitignore`, and `git://` resources expose repository
  data to regions through vendored libgit2.
- **Extensions.** Separate OS processes speaking [protocol v1](docs/protocol.md)
  over stdio, registered in `insitu.toml` and installed with
  `insitu extension add`. They may call back into the host for resources.

## 6. Deployment and distribution

Every channel ships the same native binary: crates.io, PyPI wheels, npm platform
packages and a Nix flake. Pushing a `v*` tag runs `.github/workflows/release.yml`,
which publishes through trusted publishing (no stored tokens) after a reviewer
approves the `release` environment. Pull requests that touch release inputs run
the same pipeline as a dry run. Details are in [distribution](docs/distribution.md).

## 7. Security considerations

- Extensions are trusted processes with the user's privileges. Permissions restrict
  host-mediated requests; they are not an operating-system sandbox.
- SQL models must be read-only `SELECT`/`WITH` queries and run under a SQLite
  authorizer.
- Reads and writes are confined to the project root, and the models and templates
  directories are never write targets.
- Writes check each file's original bytes first, so a file edited mid-pass is not
  overwritten.

## 8. Development and testing

[devenv](https://devenv.sh) pins every tool; `direnv allow` enters the shell and
installs the Git hooks, and `devenv up` runs `insitu watch`. `just check` runs every
gate: rustfmt, Clippy, nextest and doctests, rustdoc, cargo-deny and cargo-machete,
Ruff and pytest for the Python SDK, Node tests for the npm launcher, rumdl,
schematter, codespell, lychee on push, and `insitu check` for this repository's own
generated regions. CI runs the gates, the minimum supported Rust version on Linux,
macOS and Windows, and the Nix build. See [CONTRIBUTING](CONTRIBUTING.md).

## 9. Future considerations

- **Incremental passes.** Every pass re-indexes and re-renders everything. This keeps
  ownership simple and avoids an untested cache, but it is a compatibility gap for
  extension invocation counts and side effects, not only a performance cost; see
  [compatibility](docs/compatibility.md).
- **Cross-file atomicity.** Writes are atomic per file, not per pass.
- **A `ProjectRoot` type**, so that only canonical roots can reach path checks.
- **Flow-style front matter** (`{a: 1}`) is refused rather than rewritten.

## 10. Project identification

- **Repository:** <https://github.com/marcusinthesky/insitu>
- **License:** see [LICENSE](LICENSE)
- **Status:** alpha; the compatibility target is the Python engine at
  `7f3a1b8327a3e14ac9ac2d8a4b25b19fcdab8daa`.

## 11. Glossary

- **Source:** an authored Markdown file selected by `[sources]` in `insitu.toml`.
- **Region:** the span between `<!-- insitu:begin KIND ... -->` and
  `<!-- insitu:end -->`, whose body Insitu owns.
- **Model:** a SQL query in `insitu/models/`; models compose with `ref()`.
- **Capability:** a named resource, transform, renderer or validator, built in or
  provided by an extension.
- **Generated front matter:** values Insitu writes under the `generated:` key.
- **Pass:** one scan, plan, render and commit over the whole project.
