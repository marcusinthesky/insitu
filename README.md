# ![insitu — Markdown in its place.](assets/insitu-logo-animated-tagline.svg)

/ɪn ˈsɪt.uː/ · in-SIT-oo

_adverb, adjective_ — from Latin _in situ_, "in its original place."

1. In the original, natural or intended position.
2. _Computing._ Keeping computed views where people and agents read them: inside the
   files themselves.

**Your files are the source of truth. Everything else is a view.**

Insitu is Git-native reactive Markdown. Write knowledge once, as ordinary Markdown
and front matter; query it with SQL, render it with templates or extensions, and
Insitu writes the result back into your Markdown files, between comment markers.
The comment-region idea comes from MagicMarkdown; Insitu adds a SQL index over every
document, generated front matter, a drift check for CI, and extensions that run as
separate processes.

The output is not a virtual view. It is Markdown that you commit, diff and review,
and it reads the same on GitHub, in your editor, in Obsidian, or to an AI agent.
No database to migrate, no plugin, no viewer.

> The source stays human-editable. The view stays machine-maintained. Both live in Git.

## See it in action

Hundreds of task files, each with front matter:

```markdown
---
title: Build authentication
status: in-progress
---
```

A SQL model over every document's front matter (`insitu init` writes this one to
`insitu/models/open_items.sql`):

```sql
select path, coalesce(title, name) as title, status
from documents
where status is not null and status != 'done'
order by path;
```

A region in your `ROADMAP.md`:

```markdown
<!-- insitu:begin sql
id = "open-items"
model = "open_items"
template = "table.md.j2"
-->
<!-- insitu:end -->
```

Run `insitu sync` and the table appears between the markers, with links relative to
the roadmap. Change a task's status and sync again, or leave `insitu watch` running
and the roadmap follows your edits. In CI, `insitu check` fails when any view is stale.

## This README maintains itself

The table below is an Insitu region. [`insitu/models/documentation.sql`](insitu/models/documentation.sql)
lists every guide in this repository and
[`documentation.md.j2`](insitu/templates/documentation.md.j2) renders it; add a
Markdown file and the next `insitu sync` adds a row, or `devenv up` adds it as you
save. A Git hook runs `insitu check`, so this list cannot fall behind the repository.

<!-- insitu:begin sql
id = "documentation"
model = "documentation"
template = "documentation.md.j2"
-->

| Guide | File |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | `ARCHITECTURE.md` |
| [Contributing](CONTRIBUTING.md) | `CONTRIBUTING.md` |
| [Compatibility](docs/compatibility.md) | `docs/compatibility.md` |
| [Distribution](docs/distribution.md) | `docs/distribution.md` |
| [Process protocol v1](docs/protocol.md) | `docs/protocol.md` |
| [Python SDK](docs/python-sdk.md) | `docs/python-sdk.md` |
| [Insitu for Node.js](packaging/npm/README.md) | `packaging/npm/README.md` |

<!-- insitu:end -->

## What it does

| Capability | What you get |
| --- | --- |
| SQL models over a SQLite index | Query Markdown and front matter like a table; models compose with `ref()` |
| `sql`, `include`, `tree`, `materialize` | Query results, reused snippets, directory trees, any resource rendered by any renderer |
| Jinja templates and `@insitu/table`, `@insitu/list`, `@insitu/json` | Any layout, or a sensible default |
| Front-matter targets | Generated values written under `generated:`, never over authored keys |
| `sync`, `check`, `watch` | Update on demand, fail CI on drift, or follow edits live |
| Git-aware scanning | `.gitignore` respected by default |
| Rust and Python extension SDKs | New transforms, resources and validators as trusted processes |
| Staged, checked writes | Each file is replaced atomically, and only if it has not changed since it was read |

The engine is a native Rust binary; Python is needed only for Python extensions.

## Built for humans, ready for agents

Coding agents already read and write Markdown. Plans, tasks, decisions and handoffs
can live as ordinary files in the repository, and Insitu keeps the indexes and
dashboards over them current, so every agent and every person reads the same state.

Insitu is not an agent framework, a memory store or a vector index. It is the
materialization layer underneath them: whatever writes the source files, Insitu keeps
the views of them in the repository.

### Let your coding agent set it up

Paste this into Claude Code, Codex, OpenCode or any agent that can work in your
repository:

```text
Set up Insitu (https://github.com/marcusinthesky/insitu) in this repository and
deliver one working, useful view, not a proposal.

1. Survey the Markdown, YAML front matter, docs, agent instructions and automation.
2. Pick the most valuable first view: a roadmap, task dashboard, docs index or similar.
3. Read Insitu's README and docs before editing. Use only directives, options and
   commands that the installed version supports; check `insitu --help`.
4. Install Insitu with a supported method, preferring the repository's pinned tooling.
5. Run `insitu init`, then write a SQL model, an optional template and one region.
6. Run `insitu sync`, confirm the result reads well without Insitu, then run
   `insitu check` and confirm it exits 0.
7. If the repository uses pre-commit or prek, add `insitu check` as a hook.
8. Summarize the files changed, the commands run, and how to add another view.

Keep authored content untouched outside Insitu regions, do not create a second
copy of data that already lives in Markdown, and do not commit or push unless asked.
```

## Usage

```sh
insitu init                          # insitu.toml plus a starter model and template
insitu sync                          # converge every region once
insitu check                         # compute without writing: 0 current, 1 stale, 2 error
insitu watch                         # converge, then react to filesystem events
insitu extension add ./my-extension  # install a process extension
```

`check` uses an in-memory index and accepts `--json`. Installation channels (Cargo,
PyPI, npm, Nix) are described in [docs/distribution.md](docs/distribution.md).

## Status

**Alpha.** This Rust engine replaces the Python engine at
`7f3a1b8327a3e14ac9ac2d8a4b25b19fcdab8daa`. Existing `insitu.toml`, region markers,
protocol v1 and the Python extension SDK are the compatibility target. On a real
project of 1,146 sources and 112 regions, both engines produce byte-identical output
in every tested scenario. That is a parity test, not a benchmark; the scenarios and
known differences are in [docs/compatibility.md](docs/compatibility.md).

Every run re-indexes and re-renders the whole project; there is no incremental cache
yet. Writes are atomic per file, **not** across files. Extensions are trusted OS
processes, not sandboxes.

## Repository

Each crate describes itself in its README's front matter, and this table, like the
one in [ARCHITECTURE.md](ARCHITECTURE.md), is generated from it:

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

`python/insitu` is the Python extension SDK (there is no Python engine),
`packaging/npm` a launcher for the native binaries, and `flake.nix` the Nix package.
[ARCHITECTURE.md](ARCHITECTURE.md) explains how the pieces fit, and
[protocol](docs/protocol.md) describes the extension wire format.

## Development

[devenv](https://devenv.sh) pins every tool through `devenv.lock`. Install the
pinned CLI with `bash nix/install-devenv.sh`, then run `direnv allow` or
`devenv shell`. Entering the shell syncs the Python SDK's dependencies from
`uv.lock` and installs the prek Git hooks; `devenv up` then runs `insitu watch`, so
generated regions follow your edits.

The Markdown gates divide the work: rumdl owns formatting and local links, lychee
external links (on push), schematter document shape
([`schemas/guide.document-schema.yaml`](schemas/guide.document-schema.yaml)),
codespell spelling, and `insitu check` generated content. Harper is the editor's
grammar checker through `harper-ls`, with `just prose` as an advisory pass.

```sh
just check        # every gate in .pre-commit-config.yaml, as the hooks run them
just test         # nextest and doctests
just lint         # Clippy, warnings denied
just parity ~/Git/pricing-perspective   # read-only `check` on a real project
just fmt          # format Rust, Nix, TOML, Markdown, Python and justfiles
just prose        # advisory Harper grammar pass over tracked Markdown
just site         # build the documentation site into site/
```

`rust-toolchain.toml` is the minimum supported Rust version, which CI tests on
Linux, macOS and Windows; the devenv shell uses a newer pinned toolchain.

---

_Your knowledge belongs in your repository. Keep it in situ._
