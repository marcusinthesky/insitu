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

On `pricing-perspective` (1,146 sources, 112 regions), `check` and `sync` match the
Python engine byte-for-byte: on the committed tree, with every region blanked, after
plan edits and with Unicode-heavy tables. Each engine also accepts the other's index.

## Known differences

| Area | Difference |
| --- | --- |
| Incrementality | Every pass rebuilds the index and renders all jobs; there is no dependency-selective invocation. Side-effectful extensions may observe the difference. |
| Templates | MiniJinja with Python-method compatibility is not all of Jinja2. Custom objects, filters, undefined-value edge cases and formatting may differ. `@insitu/json` does not sort keys or ASCII-escape as Jinja2's `tojson` does. |
| Watch | Every relevant event reruns every job, and events are not filtered by `sources.exclude`. |
| Errors | Malformed front matter, region options or an unterminated region abort the pass with exit 2 and a message naming the file, where the Python engine raised an uncaught exception (exit 1); neither writes anything. Front-matter materializations do not default `path` to the document. |
| Extension environment | `{python}` resolves to `INSITU_PYTHON`, then `VIRTUAL_ENV`, then the virtualenv the binary is installed in, then `python3`, where the Python engine always used its own interpreter. The host's `PYTHONPATH` is not forwarded. |
| YAML | `yaml-edit` preserves syntax but is not ruamel.yaml; indentation, flow/block style, tags, anchors and scalar representation may differ. |
| Formatting | Authored byte ranges are retained rather than normalizing universal newlines, which can differ on CRLF inputs. |
| Symlinks | Scanning and package copying skip or reject symlinks. |
| Includes / trees | Rust line splitting and lowercasing are not identical to Python `splitlines`/`casefold` on all Unicode. |
| Git | Unborn/detached HEAD, worktrees and history/diff parity are untested. |
| Python API | Only the extension SDK (`insitu.sdk`, `insitu.types`, `insitu.protocol`) is provided. Engine modules such as `insitu.reconcile`, `insitu.config`, `insitu.documents`, `insitu.git` and `insitu.index`, and the Typer application, are gone; integrations importing them should call the binary instead. |
| CLI presentation | Commands and exit codes match; output styling, help and shell completion do not. Prefer `--json` for scripting. |
