"""Package metadata and staging checks; no test claims to build Rust binaries."""

import importlib.util
import json
import struct
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "stage_npm", ROOT / "scripts/stage_npm.py"
)
stage_npm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_npm)


def test_versions_match():
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text())
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    npm = json.loads((ROOT / "packaging/npm/package.json").read_text())
    rust_version = cargo["workspace"]["package"]["version"]
    assert Version(rust_version) == Version(py["project"]["version"])
    assert rust_version == npm["version"]
    assert all(v == rust_version for v in npm["optionalDependencies"].values())


def test_sdk_is_included_with_native_wheel():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["tool"]["maturin"]["bindings"] == "bin"
    assert project["tool"]["maturin"]["python-packages"] == ["insitu"]
    assert (ROOT / "python/insitu/py.typed").is_file()


@pytest.mark.parametrize("target", stage_npm.TARGETS)
def test_reject_script_disguised_as_binary(target):
    with pytest.raises(ValueError):
        stage_npm.validate_binary(b"#!/bin/sh\n" + b"echo not-a-binary\n" * 10, target)


@pytest.mark.parametrize(
    "target,expected",
    [
        ("x86_64-unknown-linux-gnu", 62),
        ("aarch64-unknown-linux-gnu", 183),
    ],
)
def test_elf_header_architecture(target, expected):
    # Synthetic headers are validation inputs, never packaged as usable binaries.
    header = bytearray(128)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, expected)
    stage_npm.validate_binary(bytes(header), target)
    struct.pack_into("<H", header, 18, 0)
    with pytest.raises(ValueError):
        stage_npm.validate_binary(bytes(header), target)
