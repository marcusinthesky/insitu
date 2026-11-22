#!/usr/bin/env python3
# Adapted from Exordio for Insitu; see LICENSE-EXORDIO.
"""Explicitly normalize repository text; checks never invoke this writer."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    if args.check:
        paths = [Path(name).absolute() for name in args.paths]
    else:
        paths = repository_files()
    failed = False
    for path in paths:
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(ROOT)
        ):
            continue
        original = path.read_bytes()
        if args.check:
            problems = text_problems(original)
            for problem in problems:
                print(
                    f"{path.relative_to(ROOT)}: {problem}; run just fmt",
                    file=sys.stderr,
                )
            failed |= bool(problems)
        else:
            normalize(path, original)
    return int(failed)


def repository_files() -> list[Path]:
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    return [ROOT / os.fsdecode(name) for name in files.split(b"\0") if name]


def text_problems(content: bytes) -> list[str]:
    problems = []
    if any(line.endswith((b" ", b"\t", b"\r")) for line in content.split(b"\n")):
        problems.append("trailing whitespace")
    if content and not content.endswith(b"\n"):
        problems.append("missing final newline")
    if content.startswith(b"\xef\xbb\xbf"):
        problems.append("UTF-8 byte order marker")
    if b"\r" in content:
        problems.append("carriage return; use LF line endings")
    if content == b"\n" or content.endswith(b"\n\n"):
        problems.append("extra final blank lines")
    return problems


def normalize(path: Path, original: bytes) -> None:
    if b"\0" in original:
        return
    try:
        original.decode("utf-8-sig")
    except UnicodeDecodeError:
        return
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
    sys.exit(main())
