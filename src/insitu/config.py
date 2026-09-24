"""Project discovery and TOML configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_NAME = "insitu.toml"
DEFAULT_CONFIG = """version = 1

[sources]
include = ["**/*.md"]
respect_gitignore = true
exclude = [
  ".git/**",
  ".insitu/**",
  ".venv/**",
  ".pytest_cache/**",
  ".ruff_cache/**",
  ".pyrefly/**",
  "**/__pycache__/**",
  "dist/**",
  "site/**",
]

[paths]
models = "insitu/models"
templates = "insitu/templates"
state = ".insitu/index.db"

[watch]
debounce_ms = 25
step_ms = 5
"""
DEFAULT_MODEL = """select
  path,
  coalesce(title, name) as title,
  status
from documents
where status is not null and status != 'done'
order by path;
"""
DEFAULT_TEMPLATE = """| Item | Status |
| --- | --- |
{% for row in rows -%}
| [{{ row.title }}]({{ row.path | relative_to(this.directory) }}) | {{ row.status }} |
{% else -%}
| _None_ | |
{% endfor %}
"""


class ConfigModel(BaseModel):
    """Immutable configuration model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourcesConfig(ConfigModel):
    """Repository source selection."""

    include: tuple[str, ...] = ("**/*.md",)
    respect_gitignore: bool = True
    exclude: tuple[str, ...] = (
        ".git/**",
        ".insitu/**",
        ".venv/**",
        ".pytest_cache/**",
        ".ruff_cache/**",
        ".pyrefly/**",
        "**/__pycache__/**",
        "dist/**",
        "site/**",
    )


class PathsConfig(ConfigModel):
    """Project-relative definition and state paths."""

    models: str = "insitu/models"
    templates: str = "insitu/templates"
    state: str = ".insitu/index.db"

    @field_validator("models", "templates", "state")
    @classmethod
    def path_is_project_relative(cls, value: str) -> str:
        """Reject absolute paths and parent traversal."""
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"path must remain inside the project: {value}")
        return path.as_posix()


class WatchConfig(ConfigModel):
    """Filesystem event coalescing settings."""

    debounce_ms: int = Field(default=25, ge=0, le=10_000)
    step_ms: int = Field(default=5, ge=1, le=10_000)


class ExtensionSource(ConfigModel):
    """Configured extension path and replacement approvals."""

    path: str | None = None
    source: str | None = None
    enabled: bool = True
    allow_replace: tuple[str, ...] = ()

    @model_validator(mode="after")
    def one_location(self) -> ExtensionSource:
        """Require exactly one configured location."""
        if (self.path is None) == (self.source is None):
            raise ValueError("extension requires exactly one of path or source")
        return self


class InsituConfig(ConfigModel):
    """Validated ``insitu.toml`` document."""

    version: Literal[1] = 1
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    extensions: tuple[ExtensionSource, ...] = ()


def find_project(start: Path | None = None) -> Path:
    """Find the nearest directory containing ``insitu.toml``."""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise FileNotFoundError(f"no {CONFIG_NAME} found from {current}")


def load_config(root: Path) -> InsituConfig:
    """Load and validate project configuration."""
    with (root / CONFIG_NAME).open("rb") as stream:
        return InsituConfig.model_validate(tomllib.load(stream))


def initialize_project(root: Path, *, force: bool = False) -> tuple[Path, ...]:
    """Create the minimal usable project layout without replacing definitions."""
    root.mkdir(parents=True, exist_ok=True)
    config = root / CONFIG_NAME
    if config.exists() and not force:
        raise FileExistsError(f"{config} already exists")
    config.write_text(DEFAULT_CONFIG, encoding="utf-8")
    created = [config]
    model = root / "insitu" / "models" / "open_items.sql"
    template = root / "insitu" / "templates" / "table.md.j2"
    state = root / ".insitu"
    for path, content in ((model, DEFAULT_MODEL), (template, DEFAULT_TEMPLATE)):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            created.append(path)
    if not state.exists():
        state.mkdir(parents=True)
        created.append(state)
    gitignore = root / ".gitignore"
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".insitu/" not in current.splitlines():
        gitignore.write_text(
            f"{current.rstrip()}\n.insitu/\n".lstrip(),
            encoding="utf-8",
        )
    return tuple(created)
