{ config, pkgs, ... }:

let
  schematter = pkgs.stdenvNoCC.mkDerivation {
    pname = "schematter";
    version = "0.1.0";
    src = pkgs.fetchurl {
      url = "https://github.com/iwe-org/schematter/releases/download/schematter-v0.1.0/schematter-v0.1.0-x86_64-unknown-linux-gnu.tar.gz";
      hash = "sha256-hSEglws2SiqFQh9koaMQZUi0nKAI88QwmzCy5N6hLYM=";
    };
    dontBuild = true;
    unpackPhase = ''
      tar -xzf "$src"
    '';
    installPhase = ''
      install -Dm755 schematter "$out/bin/schematter"
    '';
    meta.mainProgram = "schematter";
  };
in
{
  name = "insitu";

  languages.python = {
    enable = true;
    package = pkgs.python313;
    venv.enable = true;
    uv = {
      enable = true;
      sync = {
        enable = true;
        allGroups = true;
      };
    };
  };

  packages = with pkgs; [
    codespell
    convco
    deadnix
    git
    just
    just-lsp
    nixfmt
    prek
    rumdl
    schematter
    statix
    tombi
    watchexec
  ];

  processes.tests = {
    exec = "watchexec --clear --restart --watch src --watch tests --watch pyproject.toml -- just test";
    cwd = config.git.root;
  };

  tasks."quality:check" = {
    exec = "prek run --all-files";
    cwd = config.git.root;
    after = [ "devenv:python:uv" ];
  };

  enterShell = ''
    prek install
  '';

  enterTest = ''
    prek run --all-files
  '';
}
