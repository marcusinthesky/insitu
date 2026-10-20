#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Install the CLI version required by the locked modules. Upstream may bump
# its CLI version before updating src/modules/latest-version on the same branch.
version=$(nix eval --impure --raw --expr '
  let input = (builtins.fromJSON (builtins.readFile ./devenv.lock)).nodes.devenv.locked;
      modules = builtins.getFlake "github:cachix/devenv/${input.rev}?dir=src/modules";
  in assert input.type == "github" && input.owner == "cachix" && input.repo == "devenv";
     builtins.readFile (modules.outPath + "/latest-version")
')
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid devenv version from locked modules" >&2; exit 1; }

if command -v devenv >/dev/null && [[ $(devenv --version) == "devenv ${version} "* ]]; then
  exit 0
fi

nix profile add --impure --accept-flake-config "github:cachix/devenv/v${version}#devenv"
