"""Location-relative repository tree transform."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pathspec import GitIgnoreSpec

from insitu.policy import PathPolicy
from insitu.types import JsonValue, Location


def _depth_option(options: dict[str, JsonValue]) -> int:
    value = options.get("depth", 1)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("tree depth must be an integer")
    return int(value)


def render_tree(
    root: Path,
    location: Location,
    options: dict[str, JsonValue],
    *,
    policy: PathPolicy,
) -> tuple[str, tuple[str, ...]]:
    """Render a compact ``tree -L N``-style code block."""
    depth = _depth_option(options)
    if not 0 <= depth <= 32:
        raise ValueError("tree depth must be between 0 and 32")
    directories_only = bool(options.get("directories_only", False))
    base = (root / location.directory).resolve()
    if not base.is_relative_to(root.resolve()):
        raise ValueError("tree location escapes project root")
    raw_exclude = options.get("exclude", [])
    if not isinstance(raw_exclude, list):
        raise TypeError("tree exclude must be an array")
    extra_exclude = GitIgnoreSpec.from_lines(
        [str(item) for item in cast("list[JsonValue]", raw_exclude)]
    )
    lines = [f"{base.name or root.name}/"]
    dependencies = (f"fs:dir:{location.directory or '.'}",)

    def walk(directory: Path, prefix: str, level: int) -> None:
        if level > depth:
            return
        entries = [
            path
            for path in directory.iterdir()
            if not policy.excluded(
                path.relative_to(root).as_posix(), is_dir=path.is_dir()
            )
            and not extra_exclude.match_file(
                path.relative_to(root).as_posix() + ("/" if path.is_dir() else "")
            )
            and (path.is_dir() or not directories_only)
        ]
        entries.sort(key=lambda path: (not path.is_dir(), path.name.casefold()))
        for index, path in enumerate(entries):
            last = index == len(entries) - 1
            marker = "└── " if last else "├── "
            lines.append(f"{prefix}{marker}{path.name}{'/' if path.is_dir() else ''}")
            if path.is_dir() and level < depth:
                walk(path, prefix + ("    " if last else "│   "), level + 1)

    if base.is_dir() and depth:
        walk(base, "", 1)
    return "```text\n" + "\n".join(lines) + "\n```", dependencies
