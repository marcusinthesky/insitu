#!/usr/bin/env python3
"""Black-box CLI safety tests and optional byte-for-byte Python-oracle comparison.

A failure is a release blocker. This script does not waive template/YAML changes.
It is intentionally separate from portable Python tests: it requires a real binary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def snapshot(root: Path, *, all_files: bool = True) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and (all_files or ".insitu" not in p.relative_to(root).parts)
    }


def invoke(
    command: list[str], action: str, root: Path, expected: int, env: dict
) -> subprocess.CompletedProcess:
    run = subprocess.run(
        [*command, action, "--root", str(root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if run.returncode != expected:
        raise AssertionError(
            f"{action} returned {run.returncode}, expected {expected}\n{run.stdout}\n{run.stderr}"
        )
    return run


def check_engine(command: list[str], directory: Path, env: dict) -> dict[str, str]:
    project = directory / "project"
    shutil.copytree(ROOT / "tests/fixtures/project", project)
    before = snapshot(project)
    invoke(command, "check", project, 1, env)
    assert snapshot(project) == before, "check modified the filesystem"
    invoke(command, "sync", project, 0, env)
    text = (project / "README.md").read_text()
    assert "[Alpha](tasks/alpha.md)" in text and "def answer()" in text
    assert "# preserve this authored comment" in text
    first = snapshot(project, all_files=False)
    invoke(command, "sync", project, 0, env)
    assert snapshot(project, all_files=False) == first, "sync is not idempotent"
    before = snapshot(project)
    invoke(command, "check", project, 0, env)
    assert snapshot(project) == before, "fresh check modified the filesystem"
    # A manually edited generated body must be repaired.
    (project / "README.md").write_text(text.replace("[Alpha]", "[Edited]"))
    invoke(command, "sync", project, 0, env)
    assert snapshot(project, all_files=False) == first
    # A self-dependency must fail before any authored write.
    readme = project / "README.md"
    readme.write_text(
        readme.read_text()
        + '\n<!-- insitu:begin include\nid="cycle"\npath="README.md"\nraw=true\n-->\nstale\n<!-- insitu:end -->\n'
    )
    before = snapshot(project, all_files=False)
    invoke(command, "sync", project, 2, env)
    assert snapshot(project, all_files=False) == before
    return first


def check_python_extension(binary: str, directory: Path, env: dict) -> None:
    directory.mkdir()
    shutil.copytree(ROOT / "tests/fixtures/extension", directory / "extension")
    (directory / "insitu.toml").write_text(
        'version=1\n[[extensions]]\npath="extension"\n'
    )
    (directory / "snippet.txt").write_text("hello from source")
    (directory / "notes.md").write_text(
        '<!-- insitu:begin read-file\nid="rpc"\npath="snippet.txt"\n-->\n<!-- insitu:end -->\n'
    )
    invoke([binary], "sync", directory, 0, env)
    assert "hello from source" in (directory / "notes.md").read_text()
    invoke([binary], "check", directory, 0, env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--oracle-python", type=Path)
    parser.add_argument(
        "--oracle-source", type=Path, help="the pinned Python checkout's src directory"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve(strict=True))
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT / "python"),
        INSITU_PYTHON=sys.executable,
        PYTHONDONTWRITEBYTECODE="1",
    )
    with tempfile.TemporaryDirectory(prefix="insitu-conformance-") as tmp:
        tmp = Path(tmp)
        candidate = check_engine([binary], tmp / "candidate", env)
        check_python_extension(binary, tmp / "extension-project", env)
        status = {
            "black_box": "passed",
            "python_extension": "passed",
            "differential": "not_run",
        }
        if args.oracle_python or args.oracle_source:
            if not (args.oracle_python and args.oracle_source):
                parser.error("both oracle options are required")
            # absolute(), not resolve(): a virtualenv interpreter is a symlink, and resolving it
            # escapes the virtualenv and its site-packages.
            oracle_env = dict(
                env,
                PYTHONPATH=str(args.oracle_source.resolve()),
                INSITU_PYTHON=str(args.oracle_python.absolute()),
            )
            oracle = check_engine(
                [str(args.oracle_python.absolute()), "-m", "insitu"],
                tmp / "oracle",
                oracle_env,
            )
            differences = sorted(
                name
                for name in set(candidate) | set(oracle)
                if candidate.get(name) != oracle.get(name)
            )
            if differences:
                raise AssertionError(
                    "Byte-for-byte oracle differences: " + ", ".join(differences)
                )
            status["differential"] = "passed"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(status, indent=2) + "\n")
        print(json.dumps(status, indent=2))
