from __future__ import annotations

from pathlib import Path
from typing import cast

import pygit2

from insitu.git import NullGitRepository, open_git_repository
from insitu.policy import PathPolicy
from insitu.types import JsonValue


def _commit(repository: pygit2.Repository, path: str, content: str) -> None:
    file_path = Path(repository.workdir) / path
    file_path.write_text(content, encoding="utf-8")
    repository.index.add(path)
    repository.index.write()
    tree = repository.index.write_tree()
    signature = pygit2.Signature("Test", "test@example.com")
    repository.create_commit(
        "HEAD",
        signature,
        signature,
        "initial",
        tree,
        [],
    )


def test_pygit2_provider_exposes_index_head_and_blob(tmp_path: Path) -> None:
    repository = pygit2.init_repository(str(tmp_path), bare=False)
    _commit(repository, "tracked.md", "tracked content")
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("ignored", encoding="utf-8")

    git = open_git_repository(tmp_path)

    assert git.tracked_files() == ("tracked.md",)
    assert git.head() is not None
    blob = cast("dict[str, JsonValue]", git.blob("HEAD", "tracked.md"))
    assert blob["content"] == "tracked content"
    status = cast("dict[str, JsonValue]", git.status("tracked.md"))
    assert status["tracked"] is True

    assert git.metadata_changed(".git/HEAD")
    assert git.dependency_changed("git://HEAD", {".git/HEAD"})
    assert git.dependency_changed("git://status/tracked.md", {"tracked.md"})


def test_path_policy_respects_git_and_hard_exclusions(tmp_path: Path) -> None:
    repository = pygit2.init_repository(str(tmp_path), bare=False)
    _commit(repository, "tracked.md", "tracked content")
    (tmp_path / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("ignored", encoding="utf-8")
    git = open_git_repository(tmp_path)
    policy = PathPolicy.create(
        tmp_path,
        exclude=(".insitu/**",),
        git=git,
        respect_gitignore=True,
        protected=(),
    )

    assert policy.excluded("ignored.md")
    assert policy.excluded(".insitu/index.db")


def test_non_git_root_uses_null_provider(tmp_path: Path) -> None:
    git = open_git_repository(tmp_path)

    assert isinstance(git, NullGitRepository)
    assert git.head() is None
    assert git.tracked_files() == ()
