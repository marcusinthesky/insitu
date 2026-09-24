"""Opt-in reconciliation budget."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_benchmark.fixture import BenchmarkFixture

pytest.importorskip("pytest_benchmark", reason="install the benchmark dependency group")

from insitu.reconcile import Reconciler

pytestmark = pytest.mark.benchmark


def test_small_project_reconcile(
    benchmark: BenchmarkFixture,
    project: Path,
) -> None:
    """Measure a full check-mode pass for the compact fixture."""

    async def run() -> None:
        reconciler = Reconciler(project, write=False)
        try:
            await reconciler.run()
        finally:
            await reconciler.close()

    benchmark(lambda: asyncio.run(run()))
