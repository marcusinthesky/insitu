# Insitu agent contract

- Read README.md, CONTRIBUTING.md, ARCHITECTURE.md and the nearest crate README.
- Use the pinned Devenv environment and repository Just commands. Run `just setup`
  explicitly to prepare dependencies and native agent links.
- Keep the four-crate Rust workspace at the root, the Python SDK under `python/`,
  and the npm launcher under `packaging/npm/`. Preserve the MSRV and release contracts.
- Keep control flow visible in `Project::run`, concrete data between subsystems,
  and pure parsing, graph and patch calculations independent of Tokio.
- Add a regression fixture for document and protocol bugs. Do not let extensions
  commit document patches through the host API.
- `just check` reports failures without repairing source. Use `just fmt` for
  formatting and `just sync` for generated Insitu regions; never edit regions by hand.
- Run `just check` and `just site` before handoff; report checks that could not run.
  Run `just security` separately when changing dependencies.
- Read `docs/exordio-adoption.md` before changing template ownership. Use only the
  guarded template update command, with an explicit Exordio commit.
- Keep credentials and user-specific agent settings out of tracked files.
