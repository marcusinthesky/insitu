"""Disposable SQLite index for repository documents."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

from pathspec import GitIgnoreSpec

from insitu.config import InsituConfig
from insitu.documents import load_document, source_body, source_frontmatter
from insitu.git import GitRepository, NullGitRepository
from insitu.policy import PathPolicy

_SCHEMA = """
pragma foreign_keys = on;
create table if not exists _documents (
  path text primary key,
  parent text not null,
  name text not null,
  stem text not null,
  depth integer not null,
  title text,
  body text not null,
  frontmatter text not null,
  content_hash text not null,
  mtime_ns integer not null,
  size integer not null
);
create table if not exists entries (
  path text primary key,
  parent text not null,
  name text not null,
  kind text not null,
  depth integer not null
);
create index if not exists entries_parent on entries(parent, kind, name);
"""
_DENIED_NAMES = (
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DELETE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_VTABLE",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_DROP_VTABLE",
    "SQLITE_ALTER_TABLE",
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
    "SQLITE_PRAGMA",
    "SQLITE_TRANSACTION",
    "SQLITE_SAVEPOINT",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
)
_DENIED = {getattr(sqlite3, name) for name in _DENIED_NAMES if hasattr(sqlite3, name)}
_FIXED_COLUMNS = (
    "path",
    "parent",
    "name",
    "stem",
    "depth",
    "title",
    "body",
    "frontmatter",
    "content_hash",
)


class RepositoryIndex:
    """SQLite-backed, rebuildable repository projection."""

    def __init__(
        self,
        root: Path,
        config: InsituConfig,
        *,
        git: GitRepository | None = None,
        memory: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.config = config
        database: str | Path = ":memory:" if memory else self.root / config.paths.state
        if isinstance(database, Path):
            database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        if not memory:
            self.connection.execute("pragma journal_mode = wal")
            self.connection.execute("pragma synchronous = normal")
        self.connection.executescript(_SCHEMA)
        self.git = git or NullGitRepository(self.root)
        self.policy = PathPolicy.create(
            self.root,
            exclude=config.sources.exclude,
            git=self.git,
            respect_gitignore=config.sources.respect_gitignore,
            protected=(config.paths.models, config.paths.templates),
        )
        self._include = GitIgnoreSpec.from_lines(config.sources.include)
        self._frontmatter_keys = self._stored_frontmatter_keys()
        self._view_dirty = True
        self._create_documents_view()
        self.last_changed_paths: frozenset[str] = frozenset()

    def close(self) -> None:
        """Close the index connection."""
        self.connection.close()

    def refresh(self, paths: Iterable[Path] | None = None) -> int:
        """Refresh sources and return the current document count."""
        changed: set[str] = set()
        if paths is None:
            present: set[str] = set()
            self.connection.execute("delete from entries")
            for directory, directories, files in self.root.walk():
                directories[:] = sorted(
                    name
                    for name in directories
                    if not self.policy.excluded(
                        (directory / name).relative_to(self.root).as_posix(),
                        is_dir=True,
                    )
                )
                for name in directories:
                    self._upsert_entry(directory / name, "directory")
                for name in sorted(files):
                    path = directory / name
                    relative = path.relative_to(self.root).as_posix()
                    if self.policy.excluded(relative):
                        continue
                    self._upsert_entry(path, "file")
                    if path.suffix.lower() != ".md" or not self._include.match_file(
                        relative
                    ):
                        continue
                    present.add(relative)
                    if self._upsert_document(path):
                        changed.add(relative)
            changed.update(self._prune_documents(present))
        else:
            for candidate in paths:
                path = candidate if candidate.is_absolute() else self.root / candidate
                relative = self._relative(path)
                if relative is None:
                    continue
                if not path.exists():
                    removed = {
                        str(row[0])
                        for row in self.connection.execute(
                            "select path from _documents where path = ? or path like ?",
                            (relative, f"{relative}/%"),
                        )
                    }
                    self.connection.execute(
                        "delete from _documents where path = ? or path like ?",
                        (relative, f"{relative}/%"),
                    )
                    self.connection.execute(
                        "delete from entries where path = ? or path like ?",
                        (relative, f"{relative}/%"),
                    )
                    changed.update(removed)
                    continue
                if self.policy.excluded(relative, is_dir=path.is_dir()):
                    continue
                if path.is_dir():
                    for nested in sorted(path.rglob("*")):
                        nested_relative = self._relative(nested)
                        if nested_relative is None or self.policy.excluded(
                            nested_relative, is_dir=nested.is_dir()
                        ):
                            continue
                        self._upsert_entry(
                            nested,
                            "directory" if nested.is_dir() else "file",
                        )
                        if (
                            nested.is_file()
                            and nested.suffix.lower() == ".md"
                            and self._include.match_file(nested_relative)
                            and self._upsert_document(nested)
                        ):
                            changed.add(nested_relative)
                    continue
                self._upsert_entry(path, "file")
                if (
                    path.is_file()
                    and path.suffix.lower() == ".md"
                    and self._include.match_file(relative)
                    and self._upsert_document(path)
                ):
                    changed.add(relative)
        self.last_changed_paths = frozenset(changed)
        self._create_documents_view()
        self.connection.commit()
        row = self.connection.execute("select count(*) from _documents").fetchone()
        return int(row[0]) if row else 0

    def document_paths(self) -> tuple[Path, ...]:
        """Return indexed Markdown source paths in stable order."""
        return tuple(
            self.root / str(row[0])
            for row in self.connection.execute(
                "select path from _documents order by path"
            )
        )

    def rows(
        self,
        sql: str,
        params: Mapping[str, object],
    ) -> list[dict[str, object]]:
        """Execute one read-only query and return mapping rows."""
        if not _looks_read_only(sql):
            msg = "SQL models must be a SELECT or WITH query"
            raise ValueError(msg)

        def authorize(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _database: str | None,
            _source: str | None,
        ) -> int:
            return sqlite3.SQLITE_DENY if action in _DENIED else sqlite3.SQLITE_OK

        self.connection.set_authorizer(authorize)
        try:
            cursor = self.connection.execute(sql, dict(params))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as error:
            raise ValueError(str(error)) from error
        finally:
            self.connection.set_authorizer(None)

    def _upsert_document(self, path: Path) -> bool:
        relative = path.relative_to(self.root).as_posix()
        stat = path.stat()
        previous = self.connection.execute(
            "select content_hash, mtime_ns, size from _documents where path = ?",
            (relative,),
        ).fetchone()
        if (
            previous is not None
            and int(previous[1]) == stat.st_mtime_ns
            and int(previous[2]) == stat.st_size
        ):
            return False
        document = load_document(path)
        frontmatter = source_frontmatter(document.frontmatter)
        body = source_body(document)
        source_hash = hashlib.sha256(
            (json.dumps(frontmatter, sort_keys=True) + body).encode()
        ).hexdigest()
        if previous is not None and str(previous[0]) == source_hash:
            self.connection.execute(
                "update _documents set mtime_ns = ?, size = ? where path = ?",
                (stat.st_mtime_ns, stat.st_size, relative),
            )
            return False
        parent = Path(relative).parent.as_posix()
        parent = "" if parent == "." else parent
        title = frontmatter.get("title")
        new_keys = set(frontmatter) - self._frontmatter_keys
        if new_keys:
            self._frontmatter_keys.update(new_keys)
            self._view_dirty = True
        self.connection.execute(
            """insert into _documents
            (path, parent, name, stem, depth, title, body, frontmatter,
             content_hash, mtime_ns, size)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(path) do update set
              parent=excluded.parent,
              name=excluded.name,
              stem=excluded.stem,
              depth=excluded.depth,
              title=excluded.title,
              body=excluded.body,
              frontmatter=excluded.frontmatter,
              content_hash=excluded.content_hash,
              mtime_ns=excluded.mtime_ns,
              size=excluded.size""",
            (
                relative,
                parent,
                path.name,
                path.stem,
                len(Path(relative).parent.parts) if parent else 0,
                str(title) if title is not None else None,
                body,
                json.dumps(frontmatter, sort_keys=True),
                source_hash,
                stat.st_mtime_ns,
                stat.st_size,
            ),
        )
        return True

    def _upsert_entry(self, path: Path, kind: str) -> None:
        relative = path.relative_to(self.root).as_posix()
        parent = Path(relative).parent.as_posix()
        parent = "" if parent == "." else parent
        self.connection.execute(
            """insert into entries(path, parent, name, kind, depth)
            values (?, ?, ?, ?, ?)
            on conflict(path) do update set
              parent=excluded.parent,
              name=excluded.name,
              kind=excluded.kind,
              depth=excluded.depth""",
            (relative, parent, path.name, kind, len(Path(relative).parts)),
        )

    def _prune_documents(self, present: set[str]) -> set[str]:
        existing = {
            str(row[0])
            for row in self.connection.execute("select path from _documents")
        }
        removed = existing - present
        self.connection.executemany(
            "delete from _documents where path = ?",
            ((path,) for path in removed),
        )
        return removed

    def _stored_frontmatter_keys(self) -> set[str]:
        keys: set[str] = set()
        for row in self.connection.execute("select frontmatter from _documents"):
            value = json.loads(row[0])
            if isinstance(value, dict):
                keys.update(map(str, value))
        return keys

    def _create_documents_view(self) -> None:
        if not self._view_dirty:
            return
        projections = list(_FIXED_COLUMNS)
        for key in sorted(self._frontmatter_keys - set(_FIXED_COLUMNS)):
            identifier = key.replace('"', '""')
            json_path = f'$."{key.replace(chr(34), chr(92) + chr(34))}"'
            literal = "'" + json_path.replace("'", "''") + "'"
            projections.append(
                f'json_extract(frontmatter, {literal}) as "{identifier}"'
            )
        self.connection.execute("drop view if exists documents")
        self.connection.execute(
            f"create view documents as select {', '.join(projections)} from _documents"
        )
        self._view_dirty = False

    def _relative(self, path: Path) -> str | None:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None


def _looks_read_only(sql: str) -> bool:
    stripped = sql.lstrip()
    while stripped.startswith("--"):
        stripped = stripped.partition("\n")[2].lstrip()
    return stripped.lower().startswith(("select", "with"))
