# Contributing

Work inside the devenv shell (see the README) and run `just check` before pushing;
the prek hooks run the same gates on commit. Keep the ordinary control flow visible
in `Project::run`. Prefer concrete structs, local borrowing and owned data between
subsystems. Introduce traits only for demonstrated variability.

Pure parsing, graph and patch calculations should not need Tokio. Keep generic
registration machinery at the SDK boundary. Do not introduce a global async mutex
around the engine or permit plugins to commit document patches through the host API.

Add a regression fixture for every document or protocol bug.
