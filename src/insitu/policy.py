"""Filesystem admission policy shared by scanning, watching, and trees."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pathspec import GitIgnoreSpec

from insitu.git import GitRepository


@dataclass(frozen=True, slots=True)
class PathPolicy:
    """Apply hard, configured, and optional Git ignore rules consistently."""

    root: Path
    excludes: GitIgnoreSpec
    git: GitRepository
    respect_gitignore: bool = True
    protected: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        exclude: Sequence[str],
        git: GitRepository,
        respect_gitignore: bool,
        protected: Sequence[str] = (),
    ) -> PathPolicy:
        """Construct a policy from configured patterns and a Git provider."""
        return cls(
            root=root.resolve(),
            excludes=GitIgnoreSpec.from_lines(exclude),
            git=git,
            respect_gitignore=respect_gitignore,
            protected=tuple(path.strip("/") for path in protected if path),
        )

    def _protected(self, relative: str) -> bool:
        normalized = relative.rstrip("/")
        return any(
            normalized == path or normalized.startswith(f"{path}/")
            for path in self.protected
        )

    def excluded(self, relative: str, *, is_dir: bool = False) -> bool:
        """Return whether a path must be pruned before source inclusion."""
        normalized = relative.rstrip("/")
        if self._protected(normalized):
            return False
        candidate = normalized + ("/" if is_dir else "")
        return self.excludes.match_file(candidate) or (
            self.respect_gitignore and self.git.is_ignored(normalized, is_dir=is_dir)
        )

    def source(self, relative: str) -> bool:
        """Return whether a non-directory path is an admitted source."""
        return not self.excluded(relative) and not self._protected(relative)

    def policy_changed(self, relative: str) -> bool:
        """Return whether an ignore-rule edit requires a full source refresh."""
        return relative == ".git/info/exclude" or Path(relative).name == ".gitignore"
