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
    actionlint
    shellcheck
    convco
    jq
    nixfmt
    statix
    deadnix
    tombi
    # Markdown: rumdl owns format and local links, lychee external links,
    # schematter document shape; harper-ls is the editor's grammar checker.
    rumdl
    codespell
    lychee
    harper
    (callPackage ./nix/schematter.nix { })
    # AgentSync also ships native macOS builds. Keep the template's package
    # definition intact while supporting Insitu's existing Darwin environment.
    ((callPackage ./infra/nix/pkgs/agentsync.nix { }).overrideAttrs (old: {
      meta = old.meta // {
        platforms = old.meta.platforms ++ [
          "x86_64-darwin"
          "aarch64-darwin"
        ];
      };
    }))
  ];
  env = {
    # uv must use this shell's Python, never a downloaded interpreter.
    UV_PYTHON_DOWNLOADS = "never";
    UV_PYTHON = "${pkgs.python313}/bin/python3";
  };
  enterShell = ''
    unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
    export PATH="$PWD/.venv/bin:$PATH"
  '';
  # `devenv up` keeps every Insitu view current while you edit.
  processes.insitu.exec = "cargo run --quiet --frozen -p insitu -- watch";
  enterTest = ''
    just check
  '';
}
