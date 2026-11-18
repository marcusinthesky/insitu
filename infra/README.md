# Shared repository infrastructure

`nix/check.sh`, `nix/check-text.sh` and `nix/pkgs/agentsync.nix` are adopted from
the Exordio revision in `.copier-answers.yml` at the repository root.
Their ownership is listed in [`exordio.toml`](../exordio.toml).

Insitu's development environment remains in root `devenv.nix`; its package,
installer and Schematter definition remain in `flake.nix` and `nix/`.
The shared AgentSync package has a small macOS platform override in `devenv.nix`
because Insitu also supports macOS development.

See the [adoption guide](../docs/exordio-adoption.md) before updating shared files.
