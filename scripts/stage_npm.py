#!/usr/bin/env python3
"""Stage an npm platform package from an already compiled executable. No downloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "x86_64-unknown-linux-gnu": ("linux-x64-gnu", "linux", "x64", "gnu"),
    "aarch64-unknown-linux-gnu": ("linux-arm64-gnu", "linux", "arm64", "gnu"),
    "x86_64-apple-darwin": ("darwin-x64", "darwin", "x64", None),
    "aarch64-apple-darwin": ("darwin-arm64", "darwin", "arm64", None),
    "x86_64-pc-windows-msvc": ("win32-x64-msvc", "win32", "x64", None),
}


def validate_binary(data: bytes, target: str) -> None:
    """Reject accidental scripts, wrong architectures and truncated headers."""
    if len(data) < 64:
        raise ValueError("executable is too small")
    _, platform, arch, _ = TARGETS[target]
    if platform == "linux":
        if data[:4] != b"\x7fELF" or data[4:6] != b"\x02\x01":
            raise ValueError("expected a 64-bit little-endian ELF executable")
        machine = struct.unpack_from("<H", data, 18)[0]
        if machine != (62 if arch == "x64" else 183):
            raise ValueError("ELF architecture does not match target")
    elif platform == "darwin":
        if data[:4] != bytes.fromhex("cffaedfe"):
            raise ValueError("expected a thin 64-bit Mach-O executable")
        cpu = struct.unpack_from("<I", data, 4)[0]
        if cpu != (0x01000007 if arch == "x64" else 0x0100000C):
            raise ValueError("Mach-O architecture does not match target")
    else:
        if data[:2] != b"MZ":
            raise ValueError("expected a PE executable")
        offset = struct.unpack_from("<I", data, 60)[0]
        if (
            offset + 6 > len(data)
            or data[offset : offset + 4] != b"PE\0\0"
            or struct.unpack_from("<H", data, offset + 4)[0] != 0x8664
        ):
            raise ValueError("invalid or non-x64 PE executable")


def stage(target: str, binary: Path, output: Path) -> Path:
    slug, platform, cpu, libc = TARGETS[target]
    data = binary.read_bytes()
    validate_binary(data, target)
    wrapper = json.loads((ROOT / "packaging/npm/package.json").read_text())
    destination = output / slug
    if destination.exists():
        raise FileExistsError(destination)
    (destination / "bin").mkdir(parents=True)
    name = "insitu.exe" if platform == "win32" else "insitu"
    executable = destination / "bin" / name
    executable.write_bytes(data)
    executable.chmod(0o755)
    manifest = {
        "name": f"@marcusinthesky/insitu-{slug}",
        "version": wrapper["version"],
        "description": f"Insitu native executable for {target}",
        "license": "MIT",
        "os": [platform],
        "cpu": [cpu],
        "files": ["bin", "SHA256SUMS", "LICENSE"],
        "publishConfig": {"access": "public", "tag": "next"},
        "repository": wrapper["repository"],
    }
    if libc:
        manifest["libc"] = ["glibc"]
    (destination / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (destination / "SHA256SUMS").write_text(
        f"{hashlib.sha256(data).hexdigest()}  bin/{name}\n"
    )
    shutil.copy2(ROOT / "LICENSE", destination / "LICENSE")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/npm")
    args = parser.parse_args()
    print(stage(args.target, args.binary, args.output))
