"""Shared test fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Copy the compact integration fixture."""
    source = Path(__file__).parent / "fixtures" / "project"
    target = tmp_path / "project"
    shutil.copytree(source, target)
    return target
