"""Project configuration tests."""

from pathlib import Path

import pytest

from insitu.config import initialize_project

pytestmark = pytest.mark.unit


def test_force_init_preserves_user_definitions(tmp_path: Path) -> None:
    """Force refreshes config but never replaces user models."""
    initialize_project(tmp_path)
    model = tmp_path / "insitu/models/open_items.sql"
    model.write_text("select 1\n", encoding="utf-8")
    initialize_project(tmp_path, force=True)
    assert model.read_text(encoding="utf-8") == "select 1\n"
