{ pkgs, ... }:
{
  name = "insitu-rust";

  # rustc, cargo, clippy, rustfmt and rust-analyzer from the pinned nixpkgs.
  # rust-toolchain.toml stays the MSRV toolchain for rustup users and CI's
  # cross-platform matrix; this shell is the newer development toolchain.
  languages.rust.enable = true;

  packages = with pkgs; [
    # Native build inputs: vendored libgit2 and bundled SQLite.
    pkg-config
    cmake
    # Rust quality gates and release tooling.
    cargo-nextest
    cargo-deny
    cargo-machete
    cargo-semver-checks
    cargo-release
    cargo-dist
    maturin
    # Python SDK and npm launcher.
    python313
    uv
    ruff
    nodejs
    # Repository gates.
    git
    gh
    just
    prek
    convco
    jq
    nixfmt
    statix
    deadnix
    tombi
    rumdl
    codespell
    lychee
  ];
  env = {
    # uv must use this shell's Python, never a downloaded interpreter.
    UV_PYTHON_DOWNLOADS = "never";
  };
  enterShell = ''
    uv sync --frozen
    export PATH="$PWD/.venv/bin:$PATH"
    if git rev-parse --git-dir >/dev/null 2>&1; then prek install; fi
  '';
  enterTest = ''
    just check
  '';
}
