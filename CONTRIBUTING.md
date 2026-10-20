# Contributing

Read the compatibility status first. Work inside the devenv shell (see the README)
and run `just check` before pushing; the prek hooks run the same gates on commit. Source changes must keep the ordinary
control flow visible in Project::run. Prefer concrete structs, local borrowing and
owned data between subsystems. Introduce traits only for demonstrated variability.

Pure parsing, graph and patch calculations should not need Tokio. Keep generic
registration machinery at the SDK boundary. Do not introduce a global async mutex
around the engine or permit plugins to commit document patches through the host API.

Required native checks are formatting, Clippy with warnings denied, workspace tests
(nextest and doctests), rustdoc and dependency policy. Portable SDK/launcher tests
are separate and are not a substitute. Run `just conformance` with the Python
oracle for any change to rendering, YAML, paths or the CLI. Add regression fixtures for every document or protocol bug. Add property
and fuzz testing for parsers and patch ranges before claiming production assurance.

A compatibility change needs a reference fixture and an explicit release decision.
Do not relax conformance assertions merely to make CI green. Release approval is
commit-bound and publication is explicit; ordinary PR workflows have read-only
repository permissions and do not receive package registry tokens.
