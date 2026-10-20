# Building and publishing

No package has been published and no registry namespace has been claimed by this
archive. Before release, confirm ownership of `insitu`, `insitu-core`,
`insitu-protocol`, `insitu-sdk` on crates.io, `insitu` on PyPI, and the proposed
`@marcusinthesky` scope on npm. Rename consistently when a namespace is unavailable.

## Locks

`Cargo.lock`, `flake.lock`, `uv.lock` and `devenv.lock` are committed and every
build runs `--locked`. Update them deliberately (`cargo update`, `nix flake update`,
`uv lock`, `devenv update`), review the diff, and rerun `just check`. Do not
generate fake checksums or use `--no-verify` to bypass a failing package.

`quality` runs every repository gate in the pinned devenv, tests the MSRV on
Linux, macOS and Windows, and checks the flake. `candidate-artifacts` is manually
dispatched and only builds/stages/uploads artifacts; it does not publish.
The Actions currently use version tags: pin reviewed commit SHAs before release.

## Cargo

Build locally with `cargo install --path crates/insitu --locked`.
Publish protocol first; SDK and core depend on protocol, CLI depends on core. Registry
index propagation can delay the next publication. `scripts/publish.py --channel
crates --evidence /path/to/evidence.json` previews commands and requires `--execute`
for mutation. A failed upload is not rolled back automatically; crate registries
and other channels do not provide a shared transaction.

## Python

Maturin uses `bindings = "bin"`; no PyO3 embedding is needed for the CLI. Wheels
contain a native `insitu` executable plus the pure-Python `insitu.sdk`, `types`
and `protocol` modules. Python extensions still require Pydantic and Python 3.13+.
`python -m insitu` forwards to the installed executable and selects the current
interpreter for `{python}` extension commands.

```sh
python -m pip install 'maturin>=1.9,<2'
maturin build --release --locked --out dist/wheels
maturin sdist --out dist/wheels
```

Install each wheel in a clean matching environment and verify CLI execution, SDK
imports, `python -m insitu`, and native conformance. Build Linux wheels using a
reviewed manylinux container/baseline for broad compatibility. The initial workflow
uses native Linux runners, so its glibc baseline and PyPI compliance are not certified.
Do not label these builds as broadly portable until auditwheel and old-baseline
installation tests pass. The provided publisher uses Twine; configure credentials
securely or adapt it to PyPI Trusted Publishing with an approved OIDC workflow.

## npm

The wrapper has no postinstall script or runtime downloads. Exact-version optional
dependencies carry binaries for Linux glibc x64/arm64, macOS x64/arm64 and Windows x64.
There is intentionally no advertised musl or Windows ARM build yet.

```sh
python scripts/stage_npm.py --target x86_64-unknown-linux-gnu \
  --binary target/x86_64-unknown-linux-gnu/release/insitu
npm pack ./packaging/npm
```

Repeat staging using the genuinely built executable for each target. Staging checks
format/architecture and emits SHA256SUMS, but cannot prove an artifact's provenance
or runtime behavior. Run native installation smoke tests before publication. Publish
all platform packages first, then the wrapper. The publisher requests npm provenance;
that requires a supported authenticated CI environment, not an ordinary local shell.
Pre-releases use the `next` tag. Change stable-release tagging deliberately.

## Nix

`nix build .`, `nix run . -- --version`, and `nix develop` are the local flake
interfaces after lock generation and fixes. Once committed to the repository and
tested, the same flake can be consumed by an exact Git revision. The Nix build uses
the Rust toolchain in the locked nixpkgs input; this is not necessarily the rustup
version used for Cargo releases. Validate both.

There is no automatic “publish to nixpkgs” operation. Inclusion in NixOS/nixpkgs needs
a packaging PR, verified release-source hash and maintainer review. Until then, the
flake is the Nix distribution surface. No nixpkgs entry or binary cache was submitted.

## Evidence and approval

Run `scripts/release_gate.py --evidence /path/to/evidence.json` from a clean Git
checkout. Evidence must identify HEAD, a maintainer, and passing Rust, docs, audit,
differential, cross-platform, package-install, Nix and namespace checks. The supplied
`verification/report.json` deliberately does not qualify. Approve only actual logs,
not an edited checklist. Store credentials outside the repository. Add signed
checksums, SBOMs and artifact attestations under the project's release policy.
