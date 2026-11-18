#!/usr/bin/env python3
"""Check BOMs, carriage returns and final blank lines without changing text."""

import sys
from pathlib import Path


def main() -> int:
    failed = False
    for name in sys.argv[1:]:
        path = Path(name)
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_bytes()
        problems = []
        if content.startswith(b"\xef\xbb\xbf"):
            problems.append("UTF-8 byte order marker")
        if b"\r" in content:
            problems.append("carriage return; use LF line endings")
        if content == b"\n" or content.endswith(b"\n\n"):
            problems.append("extra final blank lines")
        for problem in problems:
            print(f"{name}: {problem}; run just fmt", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
