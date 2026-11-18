"""Update the declared Exordio files through native Copier in a disposable copy."""

import argparse
import importlib.metadata
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

import yaml

ANSWERS = ".copier-answers.yml"
ROOT = Path(__file__).resolve().parents[1]
SAFE_PATH = re.compile(r"[A-Za-z0-9_.@/-]+")
SHA = re.compile(r"[0-9a-f]{40}")


def run(directory, *command, content=None, copier_settings=None):
    """Run without host Python settings, Git hooks, or commit signing."""
    env = os.environ.copy()
    # Git hooks can export an absolute index/worktree. No inherited Git setting
    # may redirect a disposable operation into the caller's real repository.
    for variable in tuple(env):
        if variable.startswith("GIT_") or variable in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
        }:
            env.pop(variable)
    env.update(
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="core.hooksPath",
        GIT_CONFIG_VALUE_0=os.devnull,
        GIT_CONFIG_KEY_1="commit.gpgSign",
        GIT_CONFIG_VALUE_1="false",
        GIT_AUTHOR_NAME="Exordio update check",
        GIT_AUTHOR_EMAIL="template-check@example.invalid",
        GIT_COMMITTER_NAME="Exordio update check",
        GIT_COMMITTER_EMAIL="template-check@example.invalid",
        GIT_TERMINAL_PROMPT="0",
    )
    if copier_settings is not None:
        env["COPIER_SETTINGS_PATH"] = str(copier_settings)
    return subprocess.run(
        command,
        cwd=directory,
        env=env,
        input=content,
        capture_output=True,
        check=True,
        timeout=180,
    ).stdout


def git(directory, *arguments, content=None):
    return run(directory, "git", *arguments, content=content)


def owned_paths(root):
    config = tomllib.loads((root / "exordio.toml").read_text())
    if set(config) != {"copier_version", "files"}:
        raise ValueError("exordio.toml requires only copier_version and files.")
    if importlib.metadata.version("copier") != config["copier_version"]:
        raise ValueError("Run through the locked environment: uv run --frozen.")
    paths = config["files"]
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(p, str) for p in paths)
    ):
        raise ValueError("Ownership must be a nonempty list of literal file paths.")
    if len(paths) != len(set(paths)) or ANSWERS not in paths:
        raise ValueError("Ownership must include the answers file without duplicates.")
    for name in paths:
        path = PurePosixPath(name)
        if (
            not SAFE_PATH.fullmatch(name)
            or path.is_absolute()
            or str(path) != name
            or any(part.lower() in {".", "..", ".git"} for part in path.parts)
            or name in {"exordio.toml", "scripts/template.py"}
            or any(name.startswith(other + "/") for other in paths)
        ):
            raise ValueError(f"Unsafe ownership path: {name}")
        for relative in (path, *path.parents):
            target = root / relative
            if target.is_symlink():
                raise ValueError(f"Symlink in owned path: {name}")
        target = root / path
        if target.exists() and not target.is_file():
            raise ValueError(f"Owned path is not a regular file: {name}")
    return set(paths)


def snapshot(root):
    result = {}
    names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for name in filter(None, names.decode().split("\0")):
        path = root / name
        if path.is_symlink():
            result[name] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[name] = (path.stat().st_mode & 0o777, path.read_bytes())
        elif path.exists():
            raise ValueError(f"Cannot snapshot a non-file Git entry: {name}")
    return result


def commit(root):
    git(root, "add", "--all")
    git(
        root,
        "commit",
        "--quiet",
        "--allow-empty",
        "--message",
        "test: template snapshot",
    )


def save_answers(root, answers):
    (root / ANSWERS).write_text(yaml.safe_dump(answers, sort_keys=False, indent=4))


def copier(directory, *arguments):
    # User-wide trust must not enable template migrations or Jinja extensions.
    with tempfile.TemporaryDirectory(prefix="insitu-copier-settings-") as temporary:
        settings = Path(temporary) / "settings.yml"
        settings.write_text("{}\n")
        return run(
            directory,
            sys.executable,
            "-m",
            "copier",
            *arguments,
            copier_settings=settings,
        )


def prepare(root, revision):
    """Return a checked patch and the original snapshot; never write to root."""
    if not SHA.fullmatch(revision):
        raise ValueError("Choose an explicit full 40-character commit SHA.")
    owned = owned_paths(root)
    original = snapshot(root)
    answers = yaml.safe_load((root / ANSWERS).read_text())
    if (
        not isinstance(answers, dict)
        or any(not isinstance(key, str) for key in answers)
        or not isinstance(answers.get("_commit"), str)
        or not SHA.fullmatch(answers["_commit"])
    ):
        raise ValueError("Saved Copier answers must pin a full commit SHA.")
    source = answers.get("_src_path")
    if not isinstance(source, str) or not source:
        raise ValueError("Saved Copier answers must identify their template source.")
    with tempfile.TemporaryDirectory(prefix="insitu-exordio-") as temporary:
        directory = Path(temporary)
        template, project = directory / "template", directory / "project"
        git(directory, "clone", "--quiet", "--no-checkout", "--", source, str(template))
        excluded = set()
        for index, ref in enumerate((answers["_commit"], revision)):
            resolved = (
                git(template, "rev-parse", "--verify", ref + "^{commit}")
                .decode()
                .strip()
            )
            if resolved != ref:
                raise ValueError(
                    "Pin a commit object SHA, not an annotated tag object."
                )
            rendered = directory / f"render-{index}"
            data = directory / f"answers-{index}.yml"
            data.write_text(
                yaml.safe_dump(
                    {k: v for k, v in answers.items() if not k.startswith("_")}
                )
            )
            copier(
                directory,
                "copy",
                "--defaults",
                "--skip-tasks",
                "--quiet",
                "--vcs-ref",
                ref,
                "--data-file",
                str(data),
                str(template),
                str(rendered),
            )
            for path in rendered.rglob("*"):
                if not path.is_file() and not path.is_symlink():
                    continue
                name = path.relative_to(rendered).as_posix()
                if not SAFE_PATH.fullmatch(name):
                    raise ValueError(f"Template path needs manual review: {name}")
                if name not in owned:
                    excluded.add("/" + name)
                elif path.is_symlink():
                    raise ValueError(
                        f"Template would introduce an owned symlink: {name}"
                    )
        project.mkdir()
        for name, (mode, content) in original.items():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "symlink":
                target.symlink_to(content)
            else:
                target.write_bytes(content)
                target.chmod(mode)
        git(project, "init", "--quiet")
        commit(project)
        base = git(project, "rev-parse", "HEAD").decode().strip()
        save_answers(project, {**answers, "_src_path": str(template)})
        commit(project)
        # Only positive exact paths: Copier 9.16 reorders user exclusions during
        # updates, so a '*' followed by negated allowlist entries is unsafe.
        exclusions = [part for name in sorted(excluded) for part in ("--exclude", name)]
        copier(
            project,
            "update",
            "--defaults",
            "--skip-tasks",
            "--quiet",
            "--vcs-ref",
            revision,
            *exclusions,
        )
        if git(project, "ls-files", "--unmerged"):
            raise ValueError(
                "Template update has conflicts; no repository files were changed."
            )
        updated = yaml.safe_load((project / ANSWERS).read_text())
        if (
            not isinstance(updated, dict)
            or any(not isinstance(key, str) for key in updated)
            or updated.get("_src_path") != str(template)
        ):
            raise ValueError("Template update changed its source.")
        previous_answers = {k: v for k, v in answers.items() if not k.startswith("_")}
        next_answers = {k: v for k, v in updated.items() if not k.startswith("_")}
        if next_answers != previous_answers:
            raise ValueError(
                "Changing template answers requires a separate adoption migration."
            )
        save_answers(project, {**updated, "_src_path": source, "_commit": revision})
        after = snapshot(project)
        changed = {
            name
            for name in original.keys() | after.keys()
            if original.get(name) != after.get(name)
        }
        if changed - owned:
            raise ValueError(
                f"Template changed project-owned paths: {sorted(changed - owned)}"
            )
        if any(after[name][0] == "symlink" for name in changed & after.keys()):
            raise ValueError("Template update would create an owned symlink.")
        git(project, "add", "--all")
        return git(project, "diff", "--cached", "--binary", base), original


def update(root, revision):
    if git(root, "status", "--porcelain"):
        raise ValueError("Commit or stash local changes before a template update.")
    patch, original = prepare(root, revision)
    if snapshot(root) != original or git(root, "status", "--porcelain"):
        raise ValueError(
            "Repository changed during the trial update; nothing was applied."
        )
    owned_paths(root)
    if patch:
        git(root, "apply", "--binary", "--check", content=patch)
        git(root, "apply", "--binary", content=patch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "preview", "update"))
    parser.add_argument("revision", nargs="?", help="Explicit full upstream commit SHA")
    args = parser.parse_args()
    if (args.command == "check") != (args.revision is None):
        parser.error("check takes no revision; preview and update require one.")
    try:
        owned_paths(ROOT)
        if args.command == "update":
            update(ROOT, args.revision)
            print("Updated declared Exordio files. Review the diff and run just check.")
        else:
            revision = (
                args.revision or yaml.safe_load((ROOT / ANSWERS).read_text())["_commit"]
            )
            patch, _ = prepare(ROOT, revision)
            if args.command == "check" and patch:
                raise ValueError(
                    "Saved template replay changes owned files; review with preview."
                )
            if args.command == "preview":
                sys.stdout.buffer.write(patch)
            else:
                print("Pinned Exordio replay preserves every project file.")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Template update stopped: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr.decode(errors="replace"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
