{
  # Packaging only; the development environment is devenv.nix.
  description = "Insitu native CLI";
  # The same nixpkgs revision as devenv.yaml, so the package and the shell share a toolchain.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/c043004d1c6985732bcc1cbc5a9c9aecbbb4e0f0";
  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      packageFor =
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        pkgs.rustPlatform.buildRustPackage {
          pname = "insitu";
          inherit ((pkgs.lib.importTOML ./Cargo.toml).workspace.package) version;
          src = pkgs.lib.cleanSource ./.;
          cargoLock.lockFile = ./Cargo.lock;
          cargoBuildFlags = [
            "-p"
            "insitu"
          ];
          cargoTestFlags = [ "--workspace" ];
          nativeBuildInputs = [
            pkgs.pkg-config
            pkgs.cmake
          ];
          buildInputs = pkgs.lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.libiconv ];
          doCheck = true;
          meta = {
            description = "Reactive Markdown and front-matter materializations";
            homepage = "https://github.com/marcusinthesky/insitu";
            license = pkgs.lib.licenses.mit;
            platforms = systems;
            mainProgram = "insitu";
          };
        };
    in
    {
      packages = eachSystem (system: {
        default = packageFor system;
        insitu = packageFor system;
      });
      apps = eachSystem (system: {
        default = {
          type = "app";
          program = "${packageFor system}/bin/insitu";
        };
      });
      checks = eachSystem (system: {
        build = packageFor system;
      });
    };
}
