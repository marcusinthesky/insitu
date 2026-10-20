#!/usr/bin/env python3
"""Refuse publication without explicit, commit-bound release evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "rust",
    "clippy",
    "rustdoc",
    "dependency_audit",
    "differential_compatibility",
    "linux_x64",
    "linux_arm64",
    "macos_x64",
    "macos_arm64",
    "windows_x64",
    "wheel_install",
    "npm_install",
    "nix_build",
    "registry_ownership",
}


def problems(root: Path, evidence: dict, commit: str) -> list[str]:
    errors = []
    for filename in ["Cargo.lock", "flake.lock"]:
        if not (root / filename).is_file():
            errors.append(f"missing {filename}")
    if evidence.get("commit") != commit:
        errors.append("release evidence does not match HEAD")
    if evidence.get("approved_by") in (None, "", "UNVERIFIED"):
        errors.append("missing maintainer approval")
    for name in sorted(REQUIRED):
        if evidence.get("checks", {}).get(name) != "passed":
            errors.append(f"unverified release check: {name}")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text())
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True
    ).strip():
        raise SystemExit("Publication requires a clean working tree.")
    errors = problems(ROOT, evidence, commit)
    if errors:
        raise SystemExit("RELEASE BLOCKED\n" + "\n".join(errors))
    print(f"Release evidence approved for {commit}")
