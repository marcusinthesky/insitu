# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = []
# ///
"""Refuse agent configuration collisions before AgentSync creates any links."""

import os
import stat
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CONFIGURATION = ROOT / ".agents"


def absolute(base: Path, value: str) -> Path:
    """Normalize traversal without following links that must be inspected first."""
    return Path(os.path.abspath(base / value))


def inspect(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def parent_collision(path: Path) -> str | None:
    for parent in path.parents:
        if parent == ROOT:
            break
        state = inspect(parent)
        if state and stat.S_ISLNK(state.st_mode):
            return f"{parent.relative_to(ROOT)} is a symlink"
        if state and not stat.S_ISDIR(state.st_mode):
            return f"{parent.relative_to(ROOT)} is not a directory"
    return None


def main() -> int:
    with (CONFIGURATION / "agentsync.toml").open("rb") as stream:
        configuration = tomllib.load(stream)
    source_directory = absolute(CONFIGURATION, configuration.get("source_dir", "."))
    selected = configuration.get("default_agents")
    if not isinstance(selected, list) or not selected:
        raise ValueError("default_agents must explicitly select the configured agents")
    collisions = []
    destinations = {}
    for agent in selected:
        settings = configuration.get("agents", {}).get(agent, {})
        if settings.get("enabled") is False:
            continue
        if not settings.get("targets"):
            raise ValueError(f"{agent} must explicitly configure its native targets")
        for target in settings["targets"].values():
            source = absolute(source_directory, target["source"])
            destination = absolute(ROOT, target["destination"])
            label = os.path.relpath(destination, ROOT)
            if (
                any(
                    path == ROOT or not path.is_relative_to(ROOT)
                    for path in (source, destination)
                )
                or target.get("type") != "symlink"
            ):
                collisions.append(
                    f"{label}: targets must use repository-owned sources "
                    "and native symlinks"
                )
                continue
            if destination in destinations:
                if destinations[destination] != source:
                    collisions.append(
                        f"{label}: more than one canonical source claims this target"
                    )
                continue
            destinations[destination] = source
            problem = parent_collision(source) or parent_collision(destination)
            if problem:
                collisions.append(f"{label}: {problem}")
                continue
            source_state = inspect(source)
            if source_state is None:
                collisions.append(
                    f"{label}: source {source.relative_to(ROOT)} is missing"
                )
                continue
            if stat.S_ISLNK(source_state.st_mode):
                collisions.append(
                    f"{label}: source {source.relative_to(ROOT)} is a symlink"
                )
                continue
            state = inspect(destination)
            if state is None:
                continue
            if (
                stat.S_ISLNK(state.st_mode)
                and absolute(destination.parent, os.readlink(destination)) == source
            ):
                continue
            collisions.append(
                f"{label}: preserve its contents in {source.relative_to(ROOT)} first"
            )
    if not collisions:
        return 0
    print("Agent setup stopped before changing any native targets:", file=sys.stderr)
    for collision in collisions:
        print(f"- {collision}", file=sys.stderr)
    print(
        "Merge existing instructions, settings or skills into their .agents sources, "
        "then remove the old targets only after preserving their contents. "
        "Stop tracking native targets in Git before applying managed links. "
        "See .agents/README.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Agent preflight: {error}", file=sys.stderr)
        raise SystemExit(1) from None
