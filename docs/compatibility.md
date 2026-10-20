# Compatibility

Reference: the Python engine at `marcusinthesky/insitu` commit
`7f3a1b8327a3e14ac9ac2d8a4b25b19fcdab8daa` (version 0.1.1); wire protocol 1.

## Implemented

init/sync/check/watch; TOML configuration; Markdown region parsing outside fenced
code; include/tree/sql/materialize; front-matter targets under `/generated/`;
reusable SQL models and contextual bindings; SQLite read-only authorization; Git
host resources; process manifests, dependency ordering, capability composition,
host-resource callbacks, permissions, installation and lock hashes; Rust extension
registration; and existing Python SDK imports.

On `pricing-perspective` (1,146 sources, 112 regions), `check` is clean against
content the Python engine wrote, flags the same stale region, and `sync` restores
it byte-for-byte.

## Known differences

| Area | Difference |
| --- | --- |
| Incrementality | Every pass rebuilds the index and renders all jobs; there is no dependency-selective invocation. Side-effectful extensions may observe the difference. |
| Templates | MiniJinja with Python-method compatibility is not all of Jinja2. Custom objects, filters, undefined-value edge cases and formatting may differ. `@insitu/json` does not sort keys or ASCII-escape as Jinja2's `tojson` does. |
| Watch | Every relevant event reruns every job, and events are not filtered by `sources.exclude`. |
| Errors | A malformed document aborts the whole pass with exit 2 instead of a per-file diagnostic. Front-matter materializations do not default `path` to the document. |
| Extension environment | `{python}` resolves to `INSITU_PYTHON`, then `VIRTUAL_ENV`, then `python3`; the host's `PYTHONPATH` is not forwarded. |
| YAML | `yaml-edit` preserves syntax but is not ruamel.yaml; indentation, flow/block style, tags, anchors and scalar representation may differ. |
| Formatting | Authored byte ranges are retained rather than normalizing universal newlines, which can differ on CRLF inputs. |
| Symlinks | Scanning and package copying skip or reject symlinks. |
| Includes / trees | Rust line splitting and lowercasing are not identical to Python `splitlines`/`casefold` on all Unicode. |
| Git | Unborn/detached HEAD, worktrees and history/diff parity are untested. |
| Python API | Only the extension SDK is provided. `insitu.reconcile`, `insitu.index`, `insitu.config`, `insitu.documents` and the Typer application are gone; integrations importing them should call the binary instead. |
| CLI presentation | Commands and exit codes match; output styling, help and shell completion do not. Prefer `--json` for scripting. |
