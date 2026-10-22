# Distribution

Every channel ships the same native CLI. `Cargo.lock`, `flake.lock`, `uv.lock` and
`devenv.lock` are committed and every build runs `--locked`. `candidate-artifacts`
is a manually dispatched workflow that builds wheels and staged npm packages for
five targets; it does not publish.

## Cargo

`cargo install --path crates/insitu --locked`. Publish in dependency order:
`insitu-protocol`, then `insitu-sdk` and `insitu-core`, then `insitu`.

## Python

Maturin builds `bindings = "bin"` wheels containing the native `insitu` executable
plus the pure-Python `insitu.sdk`, `types` and `protocol` modules (Python 3.13+,
Pydantic). `python -m insitu` forwards to the executable and selects the current
interpreter for `{python}` extension commands.

```sh
maturin build --release --locked --out dist/wheels
```

Wheels built on native runners are not manylinux-certified.

## npm

The wrapper has no postinstall script or runtime downloads; exact-version optional
dependencies carry binaries for Linux glibc x64/arm64, macOS x64/arm64 and Windows x64.

```sh
python scripts/stage_npm.py --target x86_64-unknown-linux-gnu \
  --binary target/x86_64-unknown-linux-gnu/release/insitu
npm pack ./packaging/npm
```

Publish all platform packages first, then the wrapper.

## Nix

`flake.nix` is for distribution only; the development shell is `devenv.nix`. It
reads the version from `Cargo.toml` and pins the same nixpkgs revision as
`devenv.yaml`. Use `nix build`, `nix run . -- --version`, or consume the flake at
a Git revision.
