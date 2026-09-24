"""Reactive, typed materializations for repository documents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("insitu")
except PackageNotFoundError:
    __version__ = "0.1.0"

__all__ = ["__version__"]
