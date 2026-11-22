# Contributing

Work inside the devenv shell (see the README), run `just setup` explicitly, and
run `just check` before pushing; the prek hooks run the same gates on commit.
Checks do not repair source. Use `just fmt` for formatting, `just sync` for
generated views, and `just security` for online dependency checks. Build the site
with `just site`.

Keep the ordinary control flow visible
in `Project::run`. Prefer concrete structs, local borrowing and owned data between
subsystems. Introduce traits only for demonstrated variability.

Pure parsing, graph and patch calculations should not need Tokio. Keep generic
registration machinery at the SDK boundary. Do not introduce a global async mutex
around the engine or permit plugins to commit document patches through the host API.

Add a regression fixture for every document or protocol bug.

## Agent setup

`just agents::apply` links the shared instructions to `AGENTS.md` and
`CLAUDE.md`. The preflight checks both destinations before creating links.
If a destination contains authored instructions, merge them into
`.agents/AGENTS.md` before clearing that destination. Use `just agents::check`
to verify the links.

If you ran setup on an earlier revision of this branch, remove obsolete
`.codex/config.toml`, `.claude/settings.json`, and either native `skills` link
only when they are symlinks pointing to the old repository sources
`.agents/codex.toml`, `.agents/claude-settings.json`, or `.agents/skills`,
respectively. Keep any personal files or links to other targets.

## Exordio tooling

The pinned source is [Exordio v0.1.2-alpha][exordio] at
`62e66f3aa0af3b5510bbe985a0f33bf45b7ff9be`. For future releases, compare the
pinned and proposed commits upstream and apply selected changes by hand.
Insitu uses the Codex and Claude integration. Language scaffolds, Markdown
workflows, and secret integration are disabled.

The retained upstream paths are relative to Exordio's `template/`:

| Upstream | Insitu |
| --- | --- |
| `.agents/agentsync.toml.copier` | `.agents/agentsync.toml`, restricted to instruction links |
| `.agents/justfile` | `.agents/justfile`, adapted to the local Python entry point |
| `src/python/scripts/agent_preflight/script.py` | `scripts/agent_preflight.py`, adapted to the local layout |
| `infra/nix/pkgs/agentsync.nix` | `nix/agentsync.nix`, with Darwin support added in `devenv.nix` |
| `infra/nix/check.sh` | `just check`, integrated with agent checks |
| `infra/nix/check-text.sh` | `scripts/format_text.py --check`, adapted to share normalization |
| `.github/workflows/security.yml` | `.github/workflows/security.yml` |

See [LICENSE-EXORDIO](LICENSE-EXORDIO) for the Apache 2.0 license covering the
imported tooling. Insitu's [MIT license](LICENSE) covers the project itself.

[exordio]: https://github.com/marcusinthesky/exordio/releases/tag/v0.1.2-alpha
