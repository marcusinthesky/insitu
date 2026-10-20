# Compatibility scope and release blockers

Reference: `marcusinthesky/insitu`, commit
`7f3a1b8327a3e14ac9ac2d8a4b25b19fcdab8daa`, Python project version 0.1.1.
Candidate: Rust 0.2.0-rc.1 / Python packaging version 0.2.0rc1; wire protocol 1.

This is not a certificate of drop-in compatibility. It records what has been
verified, the known differences, and what still blocks a release.

## Implemented

The source covers init/sync/check/watch; TOML configuration; Markdown region
parsing outside fenced code; include/tree/sql/materialize; front-matter targets
under `/generated/`; reusable SQL models and contextual bindings; SQLite read-only
authorization; Git host resources; process manifests, dependency ordering,
capability composition, host-resource callbacks, permissions, installation and
lock hashes; Rust extension registration; and existing Python SDK imports.

## Verified on Linux x86-64

- `just check`: formatting, Clippy and rustdoc with warnings denied, nextest and
  doctests, `cargo-deny`, plus the Python SDK, launcher and repository linters.
- `scripts/conformance.py` black-box, Python-extension and byte-for-byte
  differential checks against the reference source.
- `pricing-perspective` (1,146 sources, 112 regions: SQL models with Jinja
  templates, `tree`, `include`, `materialize` and Python process extensions):
  `check` is clean against content the Python engine wrote, flags the same stale
  region, and `sync` restores it byte-for-byte.
- `nix build` of the flake.

Fixes that verification required:

- MiniJinja's pycompat layer lacks `str.rsplit`, `str.ljust` and `str.rjust`. The
  renderer now adds them, tested against Jinja2 output for a real template
  (`tests/fixtures/templates`).
- pathspec matches `dir/**` against `dir/` itself; gitignore does not. Without
  emulating this, `tree` listed `.insitu/`.
- Front matter beginning with a YAML comment could not be edited.

## Known design differences and unresolved parity work

| Area | Difference / risk | Required release work |
| --- | --- | --- |
| Incrementality | Every pass rebuilds the index and renders all jobs. Watch suppresses known own writes, but does not reproduce original dependency-selective invocation counts. | Implement and test incremental selection, or explicitly release as a non-drop-in behavioral change. Side-effectful extensions may observe the difference. |
| Templates | MiniJinja with Python-method compatibility is not all of Python Jinja2. Custom Python objects, filters, undefined-value edge cases and formatting may differ. `@insitu/json` does not sort keys or ASCII-escape as Jinja2's `tojson` does. | Execute every project's template corpus against both engines; add missing `str` methods to `python_method` with Jinja2-generated expectations. |
| Watch | Every relevant event reruns every job, including side-effectful extensions (e.g. one that shells out to Nix), and events are not filtered by `sources.exclude`. | Filter events through the path policy; implement dependency-selective reruns. |
| Errors | A malformed document aborts the whole pass with exit 2 instead of a per-file diagnostic. Front-matter materializations do not default `path` to the document. | Match the Python diagnostics. |
| Extension environment | `{python}` resolves to `INSITU_PYTHON`, then `VIRTUAL_ENV`, then `python3`; the host's `PYTHONPATH` is not forwarded as Python's host does. | Document per project, or forward an explicit path list. |
| YAML | `yaml-edit` preserves syntax but is not ruamel.yaml. Generated indentation, flow/block style, tags, merge/anchor behavior and scalar representation need parity testing. | Compare complete files, not only parsed values; resolve every delta. |
| Formatting | Authored byte ranges are retained rather than Python's universal-newline normalization. This intentionally preserves more data but can differ on CRLF inputs. | Establish and test the compatibility policy. |
| Symlinks | Scan and package copying skip/reject symlinks rather than following all original cases. | Review fixtures and document security-motivated incompatibilities. |
| Includes / trees | Rust line splitting and lowercasing are not identical to Python splitlines/casefold on all Unicode. | Add boundary corpus cases; fix relevant discrepancies. |
| Git | libgit2 adapter and URI aliases are implemented, but unborn/detached HEAD, worktrees and history/diff parity are not executed. | Run Git integration tests on supported platforms. |
| Installation | PEP 440 version checks and original hash concatenation are implemented; pre-release filtering and all rollback cases need oracle testing. | Extend installer conformance tests and fault injection. |
| Python API | Public extension SDK retained; `insitu.reconcile`, `insitu.index`, the Typer application object and other Python engine internals are not provided. `pricing-perspective`'s `insitu-repository` CLI and two of its tests import `insitu.reconcile.Reconciler`, `insitu.config` and `insitu.documents`. | Do not claim compatibility for Python integrations importing those internals; such projects call the binary instead. |
| CLI presentation | Core commands/exit conventions targeted; output styling, help and shell completion are not byte-identical to Typer/Rich. | Test scripts that parse console output. Prefer structured reports. |
| Packaging | Linux binary and `nix build` verified. No wheel, npm package, macOS/Windows artifact or registry ownership check has run. | Build and smoke-test every artifact and registry channel. |

## Original authoring record

`verification/` records the portable checks run where this rewrite was first
authored, before any Rust was compiled. See `verification/report.json` and saved logs for exact counts. Python tests use
real subprocess extensions and cover schemas, typed parameters/results, callbacks,
concurrent calls, malformed frames, timeouts and SDK imports. Node tests execute a
stand-in executable to verify argument forwarding and exit status. Synthetic binary
headers test only rejection/architecture validation in the package stager.
**No stand-in or synthetic header is distributed as an Insitu executable.**

## Release is blocked until

Implement missing incremental and watch parity or change the compatibility promise.
Run the conformance and parity recipes on macOS and Windows. Build and install-test five platform
packages, wheels and Nix outputs. Verify registry ownership, licenses/advisories,
signing/provenance policy and supported platform baselines.

`release_gate.py` requires a clean commit and maintainer-approved evidence tied to
that commit. It does not turn self-reported evidence into a cryptographic proof.
Review actual CI logs and artifact attestations before granting approval.
