"""Command-line surface tests."""

import pytest
from typer.testing import CliRunner

from insitu import __version__
from insitu.cli import app

pytestmark = pytest.mark.unit


def test_version_without_command() -> None:
    """Print the version without requiring a subcommand."""
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
