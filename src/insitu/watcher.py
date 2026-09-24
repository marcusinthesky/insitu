"""Async filesystem event coalescing and watch-mode orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchfiles import Change, awatch

from insitu.config import InsituConfig
from insitu.git import GitRepository
from insitu.policy import PathPolicy
from insitu.reconcile import Reconciler
from insitu.types import ReconcileReport

ReportHandler = Callable[[ReconcileReport], Awaitable[None]]


class CoalescingQueue:
    """Bounded-by-key queue that retains only the latest event per path."""

    def __init__(self) -> None:
        self._changes: dict[Path, Change] = {}
        self._condition = asyncio.Condition()

    async def put(self, changes: set[tuple[Change, str]]) -> None:
        """Merge a watcher batch and wake one consumer."""
        async with self._condition:
            for change, raw_path in changes:
                self._changes[Path(raw_path)] = change
            self._condition.notify()

    async def get(self) -> set[Path]:
        """Take the current unique path batch without losing concurrent puts."""
        async with self._condition:
            await self._condition.wait_for(lambda: bool(self._changes))
            values = set(self._changes)
            self._changes.clear()
            return values


def _relative_path(root: Path, raw: str) -> tuple[Path, str] | None:
    path = Path(raw).resolve()
    try:
        return path, path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _filter_worktree(
    root: Path,
    policy: PathPolicy,
    changes: set[tuple[Change, str]],
) -> set[tuple[Change, str]]:
    filtered: set[tuple[Change, str]] = set()
    for change, raw in changes:
        resolved = _relative_path(root, raw)
        if resolved is not None and not policy.excluded(
            resolved[1], is_dir=resolved[0].is_dir()
        ):
            filtered.add((change, raw))
    return filtered


async def _produce_worktree(
    root: Path,
    config: InsituConfig,
    policy: PathPolicy,
    queue: CoalescingQueue,
) -> None:
    async for changes in awatch(
        root,
        debounce=config.watch.debounce_ms,
        step=config.watch.step_ms,
        watch_filter=None,
    ):
        filtered = _filter_worktree(root, policy, changes)
        if filtered:
            await queue.put(filtered)


async def _produce_git_metadata(
    root: Path,
    config: InsituConfig,
    git: GitRepository,
    queue: CoalescingQueue,
) -> None:
    git_dir = git.git_dir
    if git_dir is None:
        await asyncio.Event().wait()
        return
    root_path = root.resolve()
    try:
        git_prefix = git_dir.relative_to(root_path)
    except ValueError:
        git_prefix = Path(".git")
    async for changes in awatch(
        git_dir,
        debounce=config.watch.debounce_ms,
        step=config.watch.step_ms,
        watch_filter=None,
    ):
        filtered: set[tuple[Change, str]] = set()
        for change, raw in changes:
            if not git.metadata_changed(raw):
                continue
            relative = _relative_path(git_dir, raw)
            if relative is not None:
                filtered.add(
                    (
                        change,
                        str(root_path / git_prefix / relative[0].relative_to(git_dir)),
                    )
                )
        if filtered:
            await queue.put(filtered)


async def _consume(
    root: Path,
    policy: PathPolicy,
    reconciler: Reconciler,
    queue: CoalescingQueue,
    on_report: ReportHandler,
) -> None:
    resolved_root = root.resolve()
    while True:
        changes = await queue.get()
        relative = {
            path.resolve().relative_to(resolved_root).as_posix()
            for path in changes
            if path.resolve().is_relative_to(resolved_root)
        }
        if any(policy.policy_changed(path) for path in relative):
            await on_report(await reconciler.run())
        else:
            await on_report(await reconciler.run(changes))


async def watch_project(
    root: Path,
    config: InsituConfig,
    reconciler: Reconciler,
    on_report: ReportHandler,
) -> None:
    """Run an initial sync, then reconcile coalesced native file events."""
    queue = CoalescingQueue()
    await on_report(await reconciler.run())
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            _produce_worktree(root, config, reconciler.index.policy, queue)
        )
        tasks.create_task(_produce_git_metadata(root, config, reconciler.git, queue))
        tasks.create_task(
            _consume(root, reconciler.index.policy, reconciler, queue, on_report)
        )
