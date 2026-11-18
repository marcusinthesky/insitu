# Exordio adoption

Insitu adopts a bounded set of repository tools from
[Exordio](https://github.com/marcusinthesky/exordio), using Copier 9.16.0 and an
explicit upstream commit. The saved answers in `.copier-answers.yml`
select Codex and Claude integration, with no language scaffolds, Markdown
workflows or secret integration.

The application continues to use its four-crate Rust workspace, Python SDK,
maturin packaging, npm launcher, Nix flake and platform matrix. Selecting no
template languages means these existing components remain project-owned; it
does not disable their quality gates.

## Ownership

[`exordio.toml`](../exordio.toml) is a literal list of files that template updates
may change. New upstream files are excluded until a separate reviewed change
adds them to that list. An upstream removal can remove an owned file; it cannot
remove an Insitu-owned file.

| Owner | Files and responsibilities |
| --- | --- |
| Exordio through Copier | Listed AgentSync configuration, shared agent workflow, collision preflight, check helpers and Actions security workflow |
| Insitu | Root Just and Devenv integration, quality hooks, application and packaging, ordinary CI and releases, documentation, Nix flake, MIT license |
| Insitu contributors | Canonical instructions in `.agents/AGENTS.md`, which template updates never replace |
| AgentSync | Ignored native links under `.codex/`, `.claude/`, `AGENTS.md` and `CLAUDE.md` |
| Insitu engine | Marked documentation regions, refreshed explicitly from this workspace's Rust source |

The Exordio-derived files retain their upstream Apache 2.0 license, copied to
[`LICENSES/Exordio-Apache-2.0.txt`](../LICENSES/Exordio-Apache-2.0.txt).
Insitu's root [MIT license](../LICENSE) and package licensing are unchanged.

## Everyday commands

Enter `devenv shell`, then run `just setup`. Shell entry does not install hooks,
download dependencies or create agent links. Setup synchronizes the locked
Python development environment, fetches locked Cargo dependencies, checks for
agent configuration collisions, applies native links and installs hooks.

| Command | Purpose |
| --- | --- |
| `just check` | Check prepared dependencies, source, tests and generated views without repairing tracked files |
| `just fmt` | Explicitly format text, source and configuration |
| `just sync` | Refresh documentation regions with `cargo run --frozen -p insitu -- sync` |
| `just agents::apply` | Check collisions, then create native agent links |
| `just agents::check` | Inspect native agent link state |
| `just security` | Refresh advisory data and check Cargo dependency policy online |
| `just links` | Check external links online |
| `just site` | Build the documentation site strictly from prepared dependencies |

Ordinary checks use frozen, offline inputs. Build products, caches and ignored
virtual environments may be created; source repair and online security checks
remain explicit. CI runs setup, ordinary checks, dependency security and the
strict site build. The existing MSRV, Nix and release jobs remain in place.

Insitu bootstraps its own views from the current Rust workspace. Exordio's
published Insitu package is not installed into this repository, so a template
update cannot silently change the engine used to validate development changes.

## Update the template

Do not run bare `copier update` in this repository: the upstream template also
owns root files that Insitu has deliberately not adopted. Use the guarded
repository command from a clean branch and name a full upstream commit hash.

```sh
git switch -c chore/update-exordio
just template-preview FULL_UPSTREAM_COMMIT_SHA
just template-update FULL_UPSTREAM_COMMIT_SHA
git diff
just setup
just sync
just check
just security
just site
```

`just template-check` replays the saved revision without applying changes. It
also fetches the template, so it is separate from the ordinary offline gates.
The ownership and merge tests in `just check` use local fixture repositories.

The updater renders both the saved and requested revisions into temporary
directories, derives exclusions for every unowned output, and trials native
Copier update in a disposable Git checkout. It checks the resulting paths
against the ownership list before applying a patch. Conflicts, dirty working
trees, unsafe paths or changes outside the boundary stop the update.

The answers retain the actual upstream source and an exact commit. Template
tasks are never executed. Language and workflow selections stay empty; adding
new capabilities or changing the ownership list is an explicit migration, not
an incidental update.

The regression suite exercises real Copier updates across multiple upstream
revisions, including changed, added and removed files, and verifies that
authored project files survive. It also checks that failure leaves the real
checkout untouched. Run these tests with:

```sh
uv run --offline --frozen pytest -q tests/python/test_template.py
```

## Local adaptations

The root Just and Devenv files integrate Exordio's commands with Insitu's
existing layout. They are intentionally project-owned, so upstream changes to
those files require manual review. This adoption does not subscribe the entire
repository to future Exordio defaults.

Owned files can retain reviewed local edits, such as the TOML formatting required
by Insitu's existing gates. Copier merges those edits against the saved upstream
baseline; conflicting updates stop in the disposable checkout.

The shared AgentSync package keeps its upstream source pin. Insitu extends its
declared platforms to macOS in the root Devenv configuration, matching
AgentSync's native macOS targets; the original package file remains unchanged.
The Rust MSRV, package Nix pin and macOS/Windows test matrix are retained.
