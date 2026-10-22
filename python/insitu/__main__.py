"""Invoke the native binary, preserving the active Python for {python} extensions."""

import os
import shutil
import subprocess
import sys


def main() -> int:
    binary = os.environ.get("INSITU_BINARY_PATH") or shutil.which("insitu")
    if not binary:
        print(
            "insitu: native binary is missing; install a platform wheel or build with Cargo",
            file=sys.stderr,
        )
        return 2
    env = dict(os.environ, INSITU_PYTHON=sys.executable)
    try:
        return subprocess.call([binary, *sys.argv[1:]], env=env)
    except OSError as error:
        print(f"insitu: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
