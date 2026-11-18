{
  lib,
  rustPlatform,
  fetchFromGitHub,
  cmake,
  pkg-config,
  zlib,
  git,
}:
rustPlatform.buildRustPackage {
  pname = "agentsync";
  version = "1.45.2";

  src = fetchFromGitHub {
    owner = "dallay";
    repo = "agentsync";
    rev = "80333b6269bec5702d23f9ccca41335ddbe9bce1";
    hash = "sha256-pthsvCW3W1bUoXtng2sWcry2w5HpTKFiqiA3ndd86aM=";
  };
  cargoHash = "sha256-juIEEmDKjG5yTARQ5Zd2HBbRuYJB5ugFjjHk3GVTzDg=";

  nativeBuildInputs = [
    cmake
    pkg-config
  ];
  buildInputs = [ zlib ];
  nativeCheckInputs = [ git ];
  cargoTestFlags = [ "--lib" ];

  meta = {
    description = "Synchronize coding-agent instructions and configuration";
    homepage = "https://github.com/dallay/agentsync";
    license = lib.licenses.mit;
    mainProgram = "agentsync";
    platforms = [
      "x86_64-linux"
      "aarch64-linux"
    ];
  };
}
