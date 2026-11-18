"""Exercise repository gates in disposable projects, without compiling the engine."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def require_tools(*names):
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        pytest.skip(f"repository toolchain not installed: {', '.join(missing)}")


def run(args, project, *, env=None, check=True):
    # Git hooks export repository-local state. Disposable fixtures must never
    # inherit a caller's index, object database or worktree through that state.
    process_env = dict(os.environ if env is None else env)
    for name in list(process_env):
        if name.startswith("GIT_"):
            process_env.pop(name)
    result = subprocess.run(
        args,
        cwd=project,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture
def project(tmp_path):
    require_tools("git")
    project = tmp_path / "project"
    project.mkdir()
    run(["git", "init", "-q"], project)
    return project


def test_fixture_commands_do_not_inherit_callers_git_index(project, monkeypatch):
    caller = project.parent / "caller"
    caller.mkdir()
    run(["git", "init", "-q"], caller)
    sentinel = caller / "sentinel.txt"
    sentinel.write_text("preserve caller content\n")
    run(["git", "add", "."], caller)
    index = caller / ".git/index"
    before = index.read_bytes()
    (project / "fixture.txt").write_text("disposable fixture\n")
    monkeypatch.setenv("GIT_DIR", str(caller / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(caller))
    monkeypatch.setenv("GIT_INDEX_FILE", str(index))
    run(["git", "add", "."], project)
    assert index.read_bytes() == before
    assert sentinel.read_text() == "preserve caller content\n"
    assert run(["git", "ls-files"], project).stdout == "fixture.txt\n"


@pytest.mark.parametrize(
    "hook,content,diagnostic",
    [
        ("text-check", b"trailing space \n", "trailing whitespace"),
        ("text-check", b"missing newline", "missing final newline"),
        ("text-check", b"Windows line ending\r\n", "trailing whitespace"),
        ("text-invariants", b"\xef\xbb\xbfmarked text\n", "byte order marker"),
        ("text-invariants", b"extra final line\n\n", "extra final blank lines"),
        ("text-invariants", b"embedded\rcarriage return\n", "carriage return"),
    ],
)
def test_text_hooks_report_without_repair(project, hook, content, diagnostic):
    require_tools("bash", "prek", "python3")
    for name in [
        ".pre-commit-config.yaml",
        "infra/nix/check-text.sh",
        "scripts/check_text.py",
    ]:
        destination = project / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    source = project / "example.txt"
    source.write_bytes(content)
    env = dict(os.environ, PREK_HOME=str(project.parent / "prek-cache"))
    result = run(
        ["prek", "run", hook, "--files", "example.txt"],
        project,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert diagnostic in result.stdout + result.stderr
    assert source.read_bytes() == content


def test_text_formatter_repairs_only_repository_text(project):
    script = project / "scripts/format_text.py"
    script.parent.mkdir()
    shutil.copy2(ROOT / "scripts/format_text.py", script)
    (project / ".gitignore").write_text("ignored.txt\n")
    text = project / "example.txt"
    text.write_bytes(b"\xef\xbb\xbfhello \r\nworld\t\r\n\r\n")
    binary = project / "binary.dat"
    binary.write_bytes(b"binary\0content \r\n")
    ignored = project / "ignored.txt"
    ignored.write_bytes(b"ignored \r\n")
    outside = project.parent / "outside.txt"
    outside.write_bytes(b"outside \r\n")
    if os.name != "nt":
        (project / "linked.txt").symlink_to(outside)

    run([sys.executable, str(script)], project)
    assert text.read_bytes() == b"hello\nworld\n"
    assert binary.read_bytes() == b"binary\0content \r\n"
    assert ignored.read_bytes() == b"ignored \r\n"
    assert outside.read_bytes() == b"outside \r\n"
    assert run([sys.executable, str(script)], project).stdout == ""


def test_file_collection_keeps_untracked_and_skips_deleted_files(project):
    require_tools("bash", "python3")
    if os.name == "nt":
        pytest.skip("the repository gate runner uses Unix executable scripts")
    (project / ".gitignore").write_text("ignored.txt\n")
    (project / "tracked.txt").write_text("tracked\n")
    deleted = project / "deleted.txt"
    deleted.write_text("deleted\n")
    run(["git", "add", "."], project)
    deleted.unlink()
    (project / "untracked with spaces.txt").write_text("untracked\n")
    (project / "ignored.txt").write_text("ignored\n")
    capture = project.parent / "arguments.json"
    tools = project.parent / "tools"
    tools.mkdir()
    prek = tools / "prek"
    prek.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    prek.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(tools) + os.pathsep + os.environ["PATH"],
        CAPTURE=str(capture),
    )
    run(["bash", str(ROOT / "infra/nix/check.sh")], project, env=env)
    arguments = json.loads(capture.read_text())
    assert arguments[:2] == ["run", "--files"]
    assert set(arguments[2:]) == {
        ".gitignore",
        "tracked.txt",
        "untracked with spaces.txt",
    }
    assert not deleted.exists()


@pytest.mark.parametrize(
    "defect", ["missing-cargo", "stale-cargo", "missing-uv", "stale-uv"]
)
def test_dependency_gate_rejects_lock_defects_without_repair(project, defect):
    require_tools("bash", "just", "cargo", "uv")
    (project / "src").mkdir()
    (project / "src/lib.rs").write_text("pub fn greeting() {}\n")
    cargo = project / "Cargo.toml"
    cargo.write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
    python = project / "pyproject.toml"
    python.write_text(
        '[project]\nname = "fixture"\nversion = "0.1.0"\n'
        'requires-python = ">=3.13"\n[tool.uv]\npackage = false\n'
    )
    env = dict(
        os.environ,
        UV_PYTHON=sys.executable,
        UV_PYTHON_DOWNLOADS="never",
        UV_CACHE_DIR=str(project.parent / "uv-cache"),
        CARGO_NET_OFFLINE="true",
    )
    for name in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"]:
        env.pop(name, None)
    run(["cargo", "generate-lockfile", "--offline"], project, env=env)
    run(["uv", "lock", "--offline"], project, env=env)
    command = [
        "just",
        "--justfile",
        str(ROOT / "justfile"),
        "--working-directory",
        str(project),
        "dependencies",
    ]
    run(command, project, env=env)
    if defect == "missing-cargo":
        (project / "Cargo.lock").unlink()
    elif defect == "stale-cargo":
        cargo.write_text(cargo.read_text().replace("0.1.0", "0.2.0"))
    elif defect == "missing-uv":
        (project / "uv.lock").unlink()
    else:
        python.write_text(python.read_text().replace("0.1.0", "0.2.0"))
    before = {
        path.name: path.read_bytes() for path in project.iterdir() if path.is_file()
    }
    result = run(command, project, env=env, check=False)
    assert result.returncode != 0, "a missing or stale lock must fail the gate"
    after = {
        path.name: path.read_bytes() for path in project.iterdir() if path.is_file()
    }
    assert after == before
