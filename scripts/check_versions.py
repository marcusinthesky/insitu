#!/usr/bin/env python3
"""Fail unless Cargo, PyPI and npm (and optionally a release tag) agree on the version."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRERELEASE = {"alpha": "a", "beta": "b", "rc": "rc"}


def pep440(semver: str) -> str:
    """Map a SemVer pre-release such as 0.2.0-rc.1 to its PEP 440 spelling 0.2.0rc1."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?", semver)
    if not match:
        raise ValueError(f"unsupported version: {semver}")
    release, kind, number = match.groups()
    return release + (f"{PRERELEASE[kind]}{number}" if kind else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag, e.g. v0.2.0-rc.1")
    args = parser.parse_args()
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text())["workspace"]["package"][
        "version"
    ]
    npm = json.loads((ROOT / "packaging/npm/package.json").read_text())
    found = {
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text())[
            "project"
        ]["version"],
        "packaging/npm/package.json": npm["version"],
    }
    expected = {"pyproject.toml": pep440(cargo), "packaging/npm/package.json": cargo}
    for name, version in npm.get("optionalDependencies", {}).items():
        found[name] = version
        expected[name] = cargo
    if args.tag is not None:
        found["tag"] = args.tag
        expected["tag"] = f"v{cargo}"
    mismatches = [
        f"{k}: {found[k]} (expected {expected[k]})"
        for k in found
        if found[k] != expected[k]
    ]
    for line in mismatches:
        print(f"version mismatch with Cargo.toml {cargo}: {line}", file=sys.stderr)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
