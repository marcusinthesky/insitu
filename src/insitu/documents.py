"""Markdown regions and round-trip YAML front-matter patches."""

from __future__ import annotations

import io
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from insitu.types import FrontMatterDeletePatch, FrontMatterSetPatch, JsonValue

_BEGIN = re.compile(
    r"<!--\s*insitu:begin(?:\s+(?P<kind>[\w.-]+))?\s*\n(?P<header>.*?)-->",
    re.DOTALL,
)
_END = re.compile(r"<!--\s*insitu:end\s*-->")
_YAML = YAML(typ="rt")
_YAML.preserve_quotes = True
_YAML.width = 4096


@dataclass(frozen=True, slots=True)
class Region:
    """One managed Markdown region and its source ranges."""

    id: str
    kind: str
    options: dict[str, JsonValue]
    line: int
    content_start: int
    content_end: int


@dataclass(slots=True)
class MarkdownDocument:
    """Parsed repository Markdown document."""

    path: Path
    text: str
    frontmatter: CommentedMap
    body_offset: int
    regions: tuple[Region, ...]


def load_document(path: Path) -> MarkdownDocument:
    """Load front matter and managed regions from ``path``."""
    text = path.read_text(encoding="utf-8")
    frontmatter, body_offset = parse_frontmatter(text)
    return MarkdownDocument(
        path=path,
        text=text,
        frontmatter=frontmatter,
        body_offset=body_offset,
        regions=parse_regions(text, body_offset),
    )


def parse_frontmatter(text: str) -> tuple[CommentedMap, int]:
    """Return a round-trip YAML map and the body byte offset."""
    if not text.startswith("---\n"):
        return CommentedMap(), 0
    match = re.search(r"^---\s*$", text[4:], re.MULTILINE)
    if match is None:
        return CommentedMap(), 0
    end = 4 + match.end()
    if end < len(text) and text[end] == "\n":
        end += 1
    raw = text[4 : 4 + match.start()]
    loaded = _YAML.load(raw) or CommentedMap()
    if not isinstance(loaded, CommentedMap):
        msg = "YAML front matter must be a mapping"
        raise ValueError(msg)
    return loaded, end


def parse_regions(text: str, start: int = 0) -> tuple[Region, ...]:
    """Parse managed regions outside Markdown fenced code blocks."""
    regions: list[Region] = []
    fences = _fenced_ranges(text, start)
    cursor = start
    ordinal = 0
    while begin := _next_outside(_BEGIN, text, cursor, fences):
        end = _next_outside(_END, text, begin.end(), fences)
        if end is None:
            line = text.count("\n", 0, begin.start()) + 1
            raise ValueError(f"unterminated Insitu region at line {line}")
        options = cast(
            "dict[str, JsonValue]",
            tomllib.loads(begin.group("header").strip()),
        )
        ordinal += 1
        regions.append(
            Region(
                id=str(options.get("id") or f"region-{ordinal}"),
                kind=begin.group("kind") or str(options.get("kind") or "sql"),
                options=options,
                line=text.count("\n", 0, begin.start()) + 1,
                content_start=begin.end(),
                content_end=end.start(),
            )
        )
        cursor = end.end()
    return tuple(regions)


def replace_regions(text: str, replacements: dict[str, str]) -> str:
    """Replace managed-region contents while preserving directives."""
    regions = parse_regions(text)
    updated = text
    for region in reversed(regions):
        if region.id not in replacements:
            continue
        content = replacements[region.id].rstrip()
        rendered = f"\n\n{content}\n\n" if content else "\n\n"
        updated = (
            updated[: region.content_start] + rendered + updated[region.content_end :]
        )
    return updated


def source_text(text: str) -> str:
    """Remove managed bodies while retaining their source directives."""
    updated = text
    for region in reversed(parse_regions(text)):
        updated = updated[: region.content_start] + "\n" + updated[region.content_end :]
    return updated


def source_body(document: MarkdownDocument) -> str:
    """Return queryable body text without generated region contents."""
    body = document.text[document.body_offset :]
    offset = document.body_offset
    for region in reversed(document.regions):
        start = region.content_start - offset
        end = region.content_end - offset
        body = body[:start] + "\n" + body[end:]
    return body


def source_frontmatter(value: CommentedMap) -> dict[str, JsonValue]:
    """Return queryable front matter without Insitu-owned fields."""
    return {
        str(key): _json_value(item)
        for key, item in value.items()
        if str(key) not in {"insitu", "generated"}
    }


def frontmatter_materializations(
    path: str,
    frontmatter: CommentedMap,
) -> tuple[dict[str, JsonValue], ...]:
    """Read declarative materializations from front matter."""
    config = frontmatter.get("insitu")
    if not isinstance(config, dict):
        return ()
    raw = config.get("materializations")
    if not isinstance(raw, dict):
        return ()
    values: list[dict[str, JsonValue]] = []
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        item = {str(key): _json_value(entry) for key, entry in value.items()}
        item.setdefault("id", str(name))
        item.setdefault("path", path)
        values.append(item)
    return tuple(values)


def apply_frontmatter_patches(
    text: str,
    patches: list[FrontMatterSetPatch | FrontMatterDeletePatch],
) -> str:
    """Apply typed front-matter patches and round-trip the document."""
    frontmatter, offset = parse_frontmatter(text)
    pointers = [patch.pointer.rstrip("/") for patch in patches]
    for index, left in enumerate(pointers):
        for right in pointers[index + 1 :]:
            overlaps = (
                left == right
                or left.startswith(f"{right}/")
                or right.startswith(f"{left}/")
            )
            if overlaps:
                raise ValueError(f"overlapping front-matter targets: {left}, {right}")
    for patch in patches:
        parts = _pointer_parts(patch.pointer)
        if isinstance(patch, FrontMatterSetPatch):
            _set_pointer(frontmatter, parts, patch.value)
        else:
            _delete_pointer(frontmatter, parts)
    stream = io.StringIO()
    _YAML.dump(frontmatter, stream)
    rendered = stream.getvalue()
    body = text[offset:] if offset else text
    return f"---\n{rendered}---\n{body.lstrip(chr(10))}"


def atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` atomically with UTF-8 text."""
    temporary = path.with_name(f".{path.name}.insitu.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _next_outside(
    pattern: re.Pattern[str],
    text: str,
    start: int,
    fences: tuple[tuple[int, int], ...],
) -> re.Match[str] | None:
    cursor = start
    while match := pattern.search(text, cursor):
        if not any(left <= match.start() < right for left, right in fences):
            return match
        cursor = match.end()
    return None


def _fenced_ranges(text: str, start: int) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    opened: tuple[str, int, int] | None = None
    offset = start
    for line in text[start:].splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw[indent:] if indent <= 3 else ""
        marker = stripped[:1]
        length = (
            len(stripped) - len(stripped.lstrip(marker)) if marker in {"`", "~"} else 0
        )
        if opened is None and length >= 3:
            opened = marker, length, offset
        elif opened is not None:
            character, minimum, left = opened
            if (
                marker == character
                and length >= minimum
                and not stripped[length:].strip()
            ):
                ranges.append((left, offset + len(line)))
                opened = None
        offset += len(line)
    if opened is not None:
        ranges.append((opened[2], len(text)))
    return tuple(ranges)


def _pointer_parts(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/"):
        msg = f"front-matter pointer must start with '/': {pointer}"
        raise ValueError(msg)
    return tuple(
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    )


def _set_pointer(
    target: CommentedMap,
    parts: tuple[str, ...],
    value: JsonValue,
) -> None:
    if not parts or parts == ("",):
        msg = "front-matter root cannot be replaced"
        raise ValueError(msg)
    cursor: CommentedMap = target
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, CommentedMap):
            child = CommentedMap()
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _delete_pointer(target: CommentedMap, parts: tuple[str, ...]) -> None:
    if not parts:
        return
    cursor: Any = target
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return
        cursor = cursor[part]
    if isinstance(cursor, dict):
        cursor.pop(parts[-1], None)


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)
