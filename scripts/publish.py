#!/usr/bin/env python3
"""Explicit registry publication after release approval. Never runs in ordinary CI.

Default operation is a command preview, not publication. --execute is required.
This does not submit to nixpkgs: flake distribution and nixpkgs inclusion differ.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--evidence", type=Path, required=True)
parser.add_argument("--channel", choices=["crates", "npm", "pypi"], required=True)
parser.add_argument("--execute", action="store_true")
args = parser.parse_args()
subprocess.run(
    [
        sys.executable,
        str(ROOT / "scripts/release_gate.py"),
        "--evidence",
        str(args.evidence.resolve()),
    ],
    cwd=ROOT,
    check=True,
)
commands = []
if args.channel == "crates":
    # Registry availability can lag publication; rerun the next package once its
    # dependencies appear. Never use --no-verify to hide a packaging failure.
    for package in ["insitu-protocol", "insitu-sdk", "insitu-core", "insitu"]:
        commands.append(["cargo", "publish", "--locked", "-p", package])
elif args.channel == "npm":
    wrapper = ROOT / "packaging/npm/package.json"
    manifest = json.loads(wrapper.read_text())
    packages = {
        json.loads(p.read_text())["name"]: p.parent
        for p in (ROOT / "dist/npm").glob("*/package.json")
    }
    for name in manifest["optionalDependencies"]:
        if name not in packages:
            raise SystemExit(f"Missing tested platform package: {name}")
        commands.append(
            [
                "npm",
                "publish",
                str(packages[name]),
                "--access",
                "public",
                "--tag",
                "next",
                "--provenance",
            ]
        )
    commands.append(
        [
            "npm",
            "publish",
            str(wrapper.parent),
            "--access",
            "public",
            "--tag",
            "next",
            "--provenance",
        ]
    )
else:
    files = sorted((ROOT / "dist/wheels").glob("*.whl"))
    if len(files) < 5:
        raise SystemExit("Expected tested wheels for all five target platforms.")
    commands.append([sys.executable, "-m", "twine", "check", *map(str, files)])
    commands.append([sys.executable, "-m", "twine", "upload", *map(str, files)])
for command in commands:
    print("+", " ".join(command), flush=True)
    if args.execute:
        subprocess.run(command, cwd=ROOT, check=True)
if not args.execute:
    print("Preview only; no registry mutation was made.")
