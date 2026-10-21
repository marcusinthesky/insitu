# schematter validates Markdown document shape; it is not in nixpkgs, so this
# pins its release binaries rather than compiling it into every shell.
# Bumping: change `version` and refresh each hash with
# `nix store prefetch-file <release asset URL>`.
{
  lib,
  stdenv,
  fetchurl,
  autoPatchelfHook,
}:
let
  version = "0.2.0";
  targets = {
    x86_64-linux = {
      target = "x86_64-unknown-linux-gnu";
      hash = "sha256-jgxWrmfWcL/7zJ7HwbFZ7hCKhtLaLeQgcVxs94u6Jdc=";
    };
    aarch64-linux = {
      target = "aarch64-unknown-linux-gnu";
      hash = "sha256-+7HwVHsgGz77l/AHscLxYnOzPagow/oDOArROuxHkng=";
    };
    x86_64-darwin = {
      target = "x86_64-apple-darwin";
      hash = "sha256-J1kNYvcyVJ2zFMw331EvAddQWDIcPef6ndaDGjPKuyo=";
    };
    aarch64-darwin = {
      target = "aarch64-apple-darwin";
      hash = "sha256-ECEKQG3lGO0LwU0C6WMzWlFi9SWCfQf8RJMuOJBentM=";
    };
  };
  platform =
    targets.${stdenv.hostPlatform.system}
      or (throw "schematter: no release binary for ${stdenv.hostPlatform.system}");
in
stdenv.mkDerivation {
  pname = "schematter";
  inherit version;
  src = fetchurl {
    url = "https://github.com/iwe-org/schematter/releases/download/schematter-v${version}/schematter-v${version}-${platform.target}.tar.gz";
    inherit (platform) hash;
  };
  sourceRoot = ".";
  # The Linux binaries link glibc and libgcc_s from the build host.
  nativeBuildInputs = lib.optional stdenv.hostPlatform.isLinux autoPatchelfHook;
  buildInputs = lib.optional stdenv.hostPlatform.isLinux stdenv.cc.cc.lib;
  installPhase = ''
    install -Dm755 schematter "$out/bin/schematter"
  '';
  meta = {
    description = "Validator for Markdown document schemas";
    homepage = "https://github.com/iwe-org/schematter";
    license = lib.licenses.asl20;
    mainProgram = "schematter";
    platforms = builtins.attrNames targets;
  };
}
