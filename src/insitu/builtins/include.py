"""Local source inclusion transform."""

from __future__ import annotations

from pathlib import Path

from insitu.types import JsonValue, Location


def _integer_option(
    options: dict[str, JsonValue],
    name: str,
    default: int,
) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"include {name} must be an integer")
    return int(value)


def render_include(
    root: Path,
    location: Location,
    options: dict[str, JsonValue],
) -> tuple[str, tuple[str, ...]]:
    """Include a whole file or selected one-based line range."""
    raw_path = str(options.get("path") or "")
    if not raw_path:
        raise ValueError("include transform requires path")
    base = root / location.directory
    candidate = (base / raw_path).resolve()
    try:
        relative = candidate.relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"included path escapes project root: {raw_path}") from error
    text = candidate.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = _integer_option(options, "start", 1)
    end = _integer_option(options, "end", len(lines))
    if start < 1 or end < start:
        raise ValueError("include lines require 1 <= start <= end")
    selected = "\n".join(lines[start - 1 : end])
    if bool(options.get("raw", False)):
        return selected, (f"fs:file:{relative}",)
    language = str(options.get("language") or candidate.suffix.lstrip("."))
    return f"```{language}\n{selected}\n```", (f"fs:file:{relative}",)
