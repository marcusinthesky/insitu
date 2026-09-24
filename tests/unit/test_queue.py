"""Async event coalescing tests."""

from pathlib import Path

import pytest
from watchfiles import Change

from insitu.watcher import CoalescingQueue

pytestmark = pytest.mark.unit


async def test_queue_keeps_one_path() -> None:
    queue = CoalescingQueue()
    await queue.put({(Change.modified, "/tmp/a"), (Change.added, "/tmp/a")})
    assert await queue.get() == {Path("/tmp/a")}
