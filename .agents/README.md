---
description: Optional native Codex and Claude configuration managed by AgentSync.
---

# Agent configuration

Canonical instructions and skills live here. Nix builds the pinned AgentSync release with Cargo. Run
`just agents::apply` to create native links.
`just agents::check` verifies their state. Setup performs no model calls.

Codex reads root AGENTS.md and its native TOML configuration; Claude reads
CLAUDE.md, skills and native settings. Shared instructions point to local
README and architecture documents rather than copying their content.

## Preserve existing instructions

Before applying links, a preflight checks every selected destination. Existing
files, skills directories, unexpected links and symlinked parent directories stop
the operation before any native target changes. Already-correct managed links are
safe to apply again. No backup or automatic merge replaces your existing rules.

When adding agents to an existing project:

1. Read each reported target and its canonical source in `.agents/`. Merge the
   instructions, settings or skills you want to keep into that source, then review
   the changes. For example, merge root `AGENTS.md` into `.agents/AGENTS.md`.
2. After preserving its contents, remove the old target. If Git tracks it, use
   `git rm PATH` so the generated native link will be ignored. Keep the canonical
   `.agents/` source under version control.
3. Run `just agents::apply`, then `just agents::check`. Review the Git diff before
   committing; native files should now link to your canonical sources.

The preflight reads `agentsync.toml`, so its targets remain the single source of
truth. It is a standard-library Python script under src/python/scripts/agent_preflight/,
run with its committed PEP 723 lock. Bun is needed only for TypeScript.
