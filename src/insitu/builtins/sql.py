"""dbt-like reusable SQL models executed by SQLite."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from insitu.graph import Dag
from insitu.index import RepositoryIndex
from insitu.types import JsonValue, Location

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_]")


@dataclass(frozen=True, slots=True)
class CompiledSql:
    """Compiled query plus its source dependencies."""

    sql: str
    files: tuple[Path, ...]
    models: tuple[str, ...]


class SqlModels:
    """Load, validate, and compile SQL models into CTEs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.environment = Environment(undefined=StrictUndefined, autoescape=False)
        self._models = self._load()

    @property
    def names(self) -> tuple[str, ...]:
        """Return available model names."""
        return tuple(sorted(self._models))

    def compile(self, name: str) -> CompiledSql:
        """Compile ``name`` and recursively inject referenced models as CTEs."""
        rendered: dict[str, str] = {}
        paths: dict[str, Path] = {}
        graph: Dag[str] = Dag()

        def visit(model: str) -> None:
            if model in rendered:
                return
            if model not in self._models:
                msg = f"unknown SQL model: {model}"
                raise KeyError(msg)
            references: list[str] = []

            def ref(dependency: str) -> str:
                references.append(dependency)
                return _alias(dependency)

            template = self.environment.from_string(self._models[model][1])
            body = template.render(ref=ref).strip().rstrip(";")
            graph.add_node(model)
            for dependency in references:
                graph.add_dependency(model, dependency)
                visit(dependency)
            rendered[model] = body
            paths[model] = self._models[model][0]

        visit(name)
        order = tuple(model for model in graph.order() if model in rendered)
        dependencies = tuple(model for model in order if model != name)
        sql = rendered[name]
        if dependencies:
            ctes = [
                f"{_alias(model)} as (\n{_indent(rendered[model])}\n)"
                for model in dependencies
            ]
            ctes.append(f"__insitu_result as (\n{_indent(rendered[name])}\n)")
            sql = "with " + ",\n".join(ctes) + "\nselect * from __insitu_result"
        return CompiledSql(
            sql=sql,
            files=tuple(paths[model] for model in order),
            models=order,
        )

    def execute(
        self,
        name: str,
        *,
        index: RepositoryIndex,
        location: Location,
        params: dict[str, JsonValue] | None = None,
    ) -> tuple[list[dict[str, object]], CompiledSql]:
        """Compile and execute a model with contextual SQLite bindings."""
        compiled = self.compile(name)
        bindings: dict[str, object] = {
            "insitu_path": location.path,
            "insitu_dir": location.directory,
            "insitu_root": location.root,
            "insitu_depth": location.depth,
        }
        bindings.update(params or {})
        return index.rows(compiled.sql, bindings), compiled

    def _load(self) -> dict[str, tuple[Path, str]]:
        models: dict[str, tuple[Path, str]] = {}
        if not self.root.exists():
            return models
        for path in self.root.rglob("*.sql"):
            name = path.relative_to(self.root).with_suffix("").as_posix()
            models[name] = (path, path.read_text(encoding="utf-8"))
        return models


def _alias(name: str) -> str:
    stem = _SAFE_NAME.sub("_", name)
    suffix = hashlib.sha1(name.encode()).hexdigest()[:8]  # noqa: S324
    return f"__insitu_{stem}_{suffix}"


def _indent(value: str) -> str:
    return "\n".join(f"  {line}" for line in value.splitlines())
