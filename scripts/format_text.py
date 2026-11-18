#!/usr/bin/env python3
"""Explicitly normalize repository text; checks never invoke this writer."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    for name in files.split(b"\0"):
        if not name:
            continue
        path = ROOT / os.fsdecode(name)
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(ROOT)
        ):
            continue
        original = path.read_bytes()
        if b"\0" in original:
            continue
        try:
            original.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        content = original.removeprefix(b"\xef\xbb\xbf")
        content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        content = b"\n".join(line.rstrip(b" \t") for line in content.split(b"\n"))
        content = content.rstrip(b"\n")
        if content:
            content += b"\n"
        if content != original:
            path.write_bytes(content)
            print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
