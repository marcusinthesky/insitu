"""Optional Git repository integration for policy and host resources."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import pygit2

from insitu.types import JsonValue

_STATUS_FLAGS = (
    (1, "index_new"),
    (2, "index_modified"),
    (4, "index_deleted"),
    (8, "index_renamed"),
    (16, "index_typechange"),
    (128, "worktree_new"),
    (256, "worktree_modified"),
    (512, "worktree_deleted"),
    (1024, "worktree_typechange"),
    (2048, "worktree_renamed"),
    (4096, "worktree_unreadable"),
    (32768, "conflicted"),
)


class GitRepository(Protocol):
    """Repository operations consumed by policy, transforms, and resources."""

    root: Path
    git_dir: Path | None

    def is_ignored(self, relative: str, *, is_dir: bool = False) -> bool:
        """Return whether a project-relative path matches Git ignores."""

    def tracked_files(self) -> tuple[str, ...]:
        """Return index-tracked paths relative to the Insitu project root."""

    def head(self) -> JsonValue:
        """Return the current HEAD record, or ``None`` outside a repository."""

    def branch(self) -> JsonValue:
        """Return the current branch record, or ``None`` outside a repository."""

    def status(self, relative: str) -> JsonValue:
        """Return status flags for one project-relative path."""

    def blob(self, revision: str, relative: str) -> JsonValue:
        """Return one revision's blob record, or ``None`` when absent."""

    def history(self, relative: str, limit: int = 20) -> JsonValue:
        """Return commits that changed one path."""

    def diff(self, relative: str) -> JsonValue:
        """Return the working-tree diff for one path."""

    def ignore_rules(self) -> JsonValue:
        """Return the paths governing Git ignore decisions."""

    def dependency_changed(self, resource: str, raw: set[str]) -> bool:
        """Return whether raw project-relative paths invalidate a Git resource."""

    def metadata_changed(self, raw: str) -> bool:
        """Return whether a raw watcher path is Git metadata."""


class NullGitRepository:
    """No-op Git provider used for projects outside a Git worktree."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.git_dir: Path | None = None

    def is_ignored(self, relative: str, *, is_dir: bool = False) -> bool:
        """Return false because no repository controls this project."""
        del relative, is_dir
        return False

    def tracked_files(self) -> tuple[str, ...]:
        """Return no tracked files outside Git."""
        return ()

    def head(self) -> JsonValue:
        """Return no HEAD outside Git."""
        return None

    def branch(self) -> JsonValue:
        """Return no branch outside Git."""
        return None

    def status(self, relative: str) -> JsonValue:
        """Return an empty status record."""
        return {"path": relative, "flags": [], "tracked": False, "ignored": False}

    def blob(self, revision: str, relative: str) -> JsonValue:
        """Return no historical blob outside Git."""
        del revision, relative
        return None

    def history(self, relative: str, limit: int = 20) -> JsonValue:
        """Return no history outside Git."""
        del relative, limit
        return []

    def diff(self, relative: str) -> JsonValue:
        """Return an empty diff outside Git."""
        return {"path": relative, "patch": ""}

    def ignore_rules(self) -> JsonValue:
        """Return no repository ignore rules."""
        return {"repository": None, "paths": []}

    def dependency_changed(self, resource: str, raw: set[str]) -> bool:
        """Report no Git resource invalidation."""
        del resource, raw
        return False

    def metadata_changed(self, raw: str) -> bool:
        """Report no Git metadata changes."""
        del raw
        return False


class Pygit2Repository:
    """Thin, project-root-relative adapter around a pygit2 repository."""

    def __init__(self, root: Path, repository: Any) -> None:  # noqa: ANN401
        self.root = root.resolve()
        self.repository = repository
        self._git_dir = Path(repository.path).resolve()
        self.git_dir: Path | None = self._git_dir
        self.repository_root = Path(repository.workdir).resolve()

    def _repository_path(self, relative: str) -> str:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"path escapes Git worktree: {relative}")
        project_path = self.root / relative_path
        if not project_path.is_relative_to(self.repository_root):
            raise ValueError(f"path escapes Git worktree: {relative}")
        return project_path.relative_to(self.repository_root).as_posix()

    def _project_path(self, repository_path: str) -> str | None:
        candidate = self.repository_root / repository_path
        if not candidate.is_relative_to(self.root):
            return None
        return candidate.relative_to(self.root).as_posix()

    def is_ignored(self, relative: str, *, is_dir: bool = False) -> bool:
        """Return whether Git ignores a project-relative path."""
        path = self._repository_path(relative)
        return bool(self.repository.path_is_ignored(path + ("/" if is_dir else "")))

    def tracked_files(self) -> tuple[str, ...]:
        """Return index-tracked files relative to the project root."""
        paths = {
            project_path
            for entry in self.repository.index
            if (project_path := self._project_path(entry.path)) is not None
        }
        return tuple(sorted(paths))

    def head(self) -> JsonValue:
        """Return the current HEAD record."""
        if self.repository.head_is_unborn:
            return None
        reference = self.repository.head
        return {
            "oid": str(reference.target),
            "ref": reference.name,
            "branch": reference.shorthand,
            "detached": bool(self.repository.head_is_detached),
        }

    def branch(self) -> JsonValue:
        """Return the current branch record."""
        head = self.head()
        if not isinstance(head, dict):
            return None
        return {
            "name": head["branch"],
            "ref": head["ref"],
            "oid": head["oid"],
            "detached": head["detached"],
        }

    def status(self, relative: str) -> JsonValue:
        """Return status flags for one project-relative path."""
        repository_path = self._repository_path(relative)
        flags = int(self.repository.status().get(repository_path, 0))
        names: list[JsonValue] = [name for mask, name in _STATUS_FLAGS if flags & mask]
        return {
            "path": relative,
            "flags": names,
            "tracked": relative in self.tracked_files(),
            "ignored": bool(flags & 16384) or self.is_ignored(relative),
        }

    def _tree_blob(self, revision: str, relative: str) -> Any | None:  # noqa: ANN401
        try:
            object_ = self.repository.revparse_single(revision)
            commit = object_.peel(type(self.repository[object_.id]))
            tree = getattr(commit, "tree", None)
            if tree is None:
                tree = object_
            return tree[self._repository_path(relative)]
        except (KeyError, ValueError, TypeError):
            return None

    def blob(self, revision: str, relative: str) -> JsonValue:
        """Return one revision's blob record."""
        blob = self._tree_blob(revision, relative)
        if blob is None or not hasattr(blob, "data"):
            return None
        return {
            "revision": revision,
            "path": relative,
            "oid": str(blob.id),
            "content": blob.data.decode("utf-8", errors="replace"),
        }

    def history(self, relative: str, limit: int = 20) -> JsonValue:
        """Return commits that changed one path."""
        if self.repository.head_is_unborn:
            return []
        previous: str | None = None
        commits: list[JsonValue] = []
        for commit in self.repository.walk(
            self.repository.head.target,
            1 << 2,  # GIT_SORT_TIME
        ):
            try:
                blob = commit.tree[self._repository_path(relative)]
                current = str(blob.id)
            except KeyError:
                current = None
            if current == previous:
                continue
            commits.append(
                {
                    "oid": str(commit.id),
                    "summary": commit.message.splitlines()[0] if commit.message else "",
                    "author": str(commit.author.name),
                    "committed_at": datetime.fromtimestamp(
                        commit.commit_time, UTC
                    ).isoformat(),
                    "path": relative,
                }
            )
            previous = current
            if len(commits) >= max(0, min(limit, 100)):
                break
        return commits

    def diff(self, relative: str) -> JsonValue:
        """Return the working-tree diff for one path."""
        repository_path = self._repository_path(relative)
        patches = [
            patch.text
            for patch in self.repository.diff("HEAD")
            if repository_path in {patch.delta.old_file.path, patch.delta.new_file.path}
        ]
        return {"path": relative, "patch": "".join(patches)}

    def ignore_rules(self) -> JsonValue:
        """Return paths governing Git ignore decisions."""
        paths: list[JsonValue] = [
            candidate.relative_to(self.root).as_posix()
            for candidate in self.root.rglob(".gitignore")
            if candidate.is_file()
        ]
        exclude = self._git_dir / "info/exclude"
        if exclude.is_file():
            paths.append(".git/info/exclude")
        return {
            "repository": str(self._git_dir),
            "paths": cast("list[JsonValue]", sorted(str(path) for path in paths)),
        }

    def metadata_changed(self, raw: str) -> bool:
        """Return whether a watcher path changes Git metadata."""
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if path == self._git_dir / "HEAD" or path == self._git_dir / "index":
            return True
        if (
            path == self._git_dir / "packed-refs"
            or path == self._git_dir / "info/exclude"
        ):
            return True
        return path.is_relative_to(self._git_dir / "refs")

    def dependency_changed(self, resource: str, raw: set[str]) -> bool:
        """Return whether raw paths invalidate a Git resource."""
        project_paths = {
            str(Path(path)) for path in raw if not path.startswith(".git/")
        }
        if resource.startswith("git://status/"):
            target = resource.removeprefix("git://status/")
            return target in project_paths or any(
                self.metadata_changed(path) for path in raw
            )
        if resource.startswith("git://diff/"):
            target = resource.removeprefix("git://diff/")
            return target in project_paths or any(
                self.metadata_changed(path) for path in raw
            )
        if resource.startswith(("git://blob/", "git://history/")):
            return any(self.metadata_changed(path) for path in raw)
        if resource in {"git://HEAD", "git://branch", "git://tracked"}:
            return any(self.metadata_changed(path) for path in raw)
        if resource == "git://ignore-rules":
            return any(
                path == ".git/info/exclude" or Path(path).name == ".gitignore"
                for path in raw
            )
        return False


def open_git_repository(root: Path) -> GitRepository:
    """Discover a worktree and return a no-op provider when none exists."""
    try:
        discovered = pygit2.discover_repository(str(root.resolve()))
        if not discovered:
            return NullGitRepository(root)
        repository = pygit2.Repository(discovered)
        if repository.workdir is None:
            return NullGitRepository(root)
        return Pygit2Repository(root, repository)
    except (OSError, ValueError):
        return NullGitRepository(root)


def git_resource(repository: GitRepository, resource: str) -> JsonValue:  # noqa: PLR0911
    """Resolve one ``git://`` host resource."""
    parsed = urlsplit(resource)
    path = parsed.path.lstrip("/")
    if resource == "git://HEAD":
        return repository.head()
    if resource == "git://branch":
        return repository.branch()
    if resource == "git://ignore-rules":
        return repository.ignore_rules()
    if resource == "git://tracked":
        return cast("list[JsonValue]", list(repository.tracked_files()))
    if path.startswith("status/"):
        return repository.status(path.removeprefix("status/"))
    if path.startswith("diff/"):
        return repository.diff(path.removeprefix("diff/"))
    if path.startswith("history/"):
        return repository.history(path.removeprefix("history/"))
    if path.startswith("blob/"):
        revision, _, relative = path.removeprefix("blob/").partition("/")
        return repository.blob(revision or "HEAD", relative)
    raise KeyError(f"unsupported Git resource: {resource}")
