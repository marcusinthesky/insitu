# Contributing

Work inside the devenv shell (see the README), run `just setup` explicitly, and
run `just check` before pushing; the prek hooks run the same gates on commit.
Checks do not repair source. Use `just fmt` for formatting, `just sync` for
generated views, and `just security` for online dependency checks. Build the site
with `just site`. Read [Exordio adoption](docs/exordio-adoption.md) before changing
template-owned files or updating the template.

Keep the ordinary control flow visible
in `Project::run`. Prefer concrete structs, local borrowing and owned data between
subsystems. Introduce traits only for demonstrated variability.

Pure parsing, graph and patch calculations should not need Tokio. Keep generic
registration machinery at the SDK boundary. Do not introduce a global async mutex
around the engine or permit plugins to commit document patches through the host API.

Add a regression fixture for every document or protocol bug.
