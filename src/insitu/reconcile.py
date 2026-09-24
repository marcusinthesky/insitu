"""Asynchronous, cycle-checked repository reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from pathspec import GitIgnoreSpec

from insitu.builtins import (
    CompiledSql,
    JinjaRenderer,
    SqlModels,
    render_include,
    render_tree,
)
from insitu.config import InsituConfig, load_config
from insitu.documents import (
    apply_frontmatter_patches,
    atomic_write,
    frontmatter_materializations,
    load_document,
    replace_regions,
    source_text,
)
from insitu.extensions import (
    CapabilityRegistry,
    ExtensionManager,
    trace_scope,
)
from insitu.git import GitRepository, git_resource, open_git_repository
from insitu.graph import Dag, DependencyCycleError
from insitu.index import RepositoryIndex
from insitu.installer import ExtensionInstaller
from insitu.types import (
    CapabilityKind,
    CapabilitySpec,
    Diagnostic,
    FrontMatterDeletePatch,
    FrontMatterSetPatch,
    JsonValue,
    Location,
    ReconcileReport,
    ResourceRead,
    Severity,
)


@dataclass(frozen=True, slots=True)
class _Job:
    path: Path
    identifier: str
    options: dict[str, JsonValue]
    line: int
    kind: str

    @property
    def key(self) -> tuple[str, str]:
        return self.path.as_posix(), self.identifier


@dataclass(frozen=True, slots=True)
class _Rendered:
    path: Path
    identifier: str
    content: str | None
    frontmatter: FrontMatterSetPatch | FrontMatterDeletePatch | None
    dependencies: tuple[str, ...]


class Reconciler:
    """Reconcile resource values with owned document targets."""

    def __init__(
        self,
        root: Path,
        *,
        write: bool,
        memory: bool | None = None,
    ) -> None:
        self.root = root.resolve()
        self.config: InsituConfig = load_config(self.root)
        self.write = write
        self.git = open_git_repository(self.root)
        index_in_memory = not write if memory is None else memory
        self.index = RepositoryIndex(
            self.root,
            self.config,
            git=self.git,
            memory=index_in_memory,
        )
        self.renderer = JinjaRenderer(self.root / self.config.paths.templates)
        self.registry = CapabilityRegistry()
        self.extensions = ExtensionManager(self.root, self.config, self.registry)
        self.graph: Dag[str] = Dag()
        self._dependencies: dict[tuple[str, str], tuple[str, ...]] = {}
        self._owned_writes: dict[str, str] = {}
        self._started = False
        self._register_builtins()

    async def start(self) -> None:
        """Start configured process extensions once."""
        if not self._started:
            ExtensionInstaller(self.root).list()
            await self.extensions.start(self._resource)
            self._started = True

    async def close(self) -> None:
        """Close extension processes and the SQLite index."""
        await self.extensions.close()
        self.index.close()

    async def run(self, changed_paths: set[Path] | None = None) -> ReconcileReport:
        """Run one all-or-nothing reconciliation pass."""
        started = time.perf_counter()
        await self.start()
        self.graph = Dag()
        scanned = self.index.refresh(changed_paths)
        self.models = SqlModels(self.root / self.config.paths.models)
        self.renderer = JinjaRenderer(self.root / self.config.paths.templates)
        diagnostics: list[Diagnostic] = []
        jobs = self._jobs(diagnostics)
        conflicts = _target_conflicts(self.root, jobs)
        diagnostics.extend(
            Diagnostic(
                severity=Severity.ERROR,
                message=f"multiple materializations own {target}",
                path=path,
            )
            for path, target in conflicts
        )
        active = {job.key for job in jobs}
        self._dependencies = {
            key: value for key, value in self._dependencies.items() if key in active
        }
        selected = self._select(jobs, changed_paths)
        if not diagnostics:
            try:
                diagnostics.extend(
                    await self.registry.validate(
                        Location(path="", directory="", root=".", depth=0)
                    )
                )
                self._seed_graph(jobs, {job.key for job in selected})
            except (
                DependencyCycleError,
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                diagnostics.append(
                    Diagnostic(severity=Severity.ERROR, message=str(error))
                )
        rendered: list[_Rendered] = []
        next_dependencies: dict[tuple[str, str], tuple[str, ...]] = {}

        async def execute(job: _Job) -> None:
            try:
                value = await self._render(job)
                rendered.append(value)
                next_dependencies[job.key] = value.dependencies
            except (
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                DependencyCycleError,
            ) as error:
                diagnostics.append(
                    Diagnostic(
                        severity=Severity.ERROR,
                        message=str(error),
                        path=job.path.relative_to(self.root).as_posix(),
                        line=job.line,
                    )
                )

        if not any(item.severity is Severity.ERROR for item in diagnostics):
            async with asyncio.TaskGroup() as tasks:
                for job in selected:
                    tasks.create_task(execute(job))
        if any(item.severity is Severity.ERROR for item in diagnostics):
            return _report(
                scanned,
                len(jobs),
                diagnostics=diagnostics,
                started=started,
            )

        self._dependencies.update(next_dependencies)
        grouped: dict[Path, list[_Rendered]] = defaultdict(list)
        for item in rendered:
            grouped[item.path].append(item)
        changed: list[str] = []
        staged: dict[Path, str] = {}
        for path, items in grouped.items():
            original = path.read_text(encoding="utf-8")
            regions = {
                item.identifier: item.content or ""
                for item in items
                if item.frontmatter is None
            }
            updated = replace_regions(original, regions) if regions else original
            frontmatter = [
                item.frontmatter for item in items if item.frontmatter is not None
            ]
            if frontmatter:
                updated = apply_frontmatter_patches(updated, frontmatter)
            if updated != original:
                relative = path.relative_to(self.root).as_posix()
                changed.append(relative)
                staged[path] = updated
        if changed and not self.write:
            diagnostics.extend(
                Diagnostic(
                    severity=Severity.ERROR,
                    message="generated content is stale; run `insitu sync`",
                    path=path,
                    code="stale",
                )
                for path in changed
            )
        elif self.write:
            for path, text in staged.items():
                atomic_write(path, text)
                relative = path.relative_to(self.root).as_posix()
                self._owned_writes[relative] = _text_hash(text)
            if staged:
                self.index.refresh(staged)
        return _report(
            scanned,
            len(jobs),
            changed=changed,
            diagnostics=diagnostics,
            started=started,
        )

    def _jobs(self, diagnostics: list[Diagnostic]) -> list[_Job]:
        jobs: list[_Job] = []
        for path in self._markdown_files():
            try:
                document = load_document(path)
                relative = path.relative_to(self.root).as_posix()
                jobs.extend(
                    _Job(path, region.id, region.options, region.line, region.kind)
                    for region in document.regions
                )
                for raw in frontmatter_materializations(relative, document.frontmatter):
                    identifier = str(raw.pop("id", "frontmatter"))
                    jobs.append(
                        _Job(
                            path,
                            identifier,
                            raw,
                            1,
                            str(raw.get("kind", "sql")),
                        )
                    )
            except (OSError, ValueError) as error:
                diagnostics.append(
                    Diagnostic(
                        severity=Severity.ERROR,
                        message=str(error),
                        path=path.relative_to(self.root).as_posix(),
                    )
                )
        return jobs

    def _select(
        self,
        jobs: list[_Job],
        changed_paths: set[Path] | None,
    ) -> list[_Job]:
        if changed_paths is None or not self._dependencies:
            return jobs
        raw = {
            path.resolve().relative_to(self.root).as_posix()
            for path in changed_paths
            if path.resolve().is_relative_to(self.root)
        }
        source = set(self.index.last_changed_paths)
        direct = set(source)
        for relative in raw - source:
            path = self.root / relative
            if path.suffix.lower() != ".md":
                continue
            actual = _file_hash(path) if path.is_file() else None
            if self._owned_writes.get(relative) != actual:
                direct.add(relative)
        return [
            job
            for job in jobs
            if job.path.relative_to(self.root).as_posix() in direct
            or _dependencies_changed(
                self._dependencies.get(job.key, ()),
                raw=raw,
                source=source,
                git=self.git,
            )
        ]

    def _seed_graph(
        self,
        jobs: Iterable[_Job],
        selected: set[tuple[str, str]],
    ) -> None:
        """Seed ownership and retained dependencies before incremental work."""
        for job in jobs:
            node, output, file_resource = _job_resources(self.root, job)
            self.graph.add_dependency(output, node)
            self.graph.add_dependency(file_resource, output)
            if job.key not in selected:
                for dependency in self._dependencies.get(job.key, ()):
                    self.graph.add_dependency(node, dependency)

    async def _render(self, job: _Job) -> _Rendered:
        relative = job.path.relative_to(self.root).as_posix()
        location = Location.from_path(relative)
        target_kind, target_pointer = _target(job.options)
        dependencies = [f"markdown:{relative}#directive:{job.identifier}"]
        node, _output, _file_resource = _job_resources(self.root, job)

        if target_kind == "frontmatter":
            if target_pointer is None:
                raise ValueError("frontmatter target requires pointer")
            if not target_pointer.startswith("/generated/"):
                raise ValueError("frontmatter targets must be under /generated/")
            if job.kind == "sql":
                rows, compiled, params = self._query(job.options, location)
                template = job.options.get("template")
                if template:
                    value: JsonValue = self.renderer.render(
                        str(template),
                        rows=rows,
                        params=params,
                        location=location,
                    )
                    if not str(template).startswith("@insitu/"):
                        dependencies.append(
                            f"fs:file:{self.config.paths.templates}/{template}"
                        )
                else:
                    value = _rows_value(rows)
                dependencies.extend(
                    f"fs:file:{item.relative_to(self.root).as_posix()}"
                    for item in compiled.files
                )
                dependencies.append("index:documents")
            elif source := job.options.get("source"):
                value, extra = await self.registry.resource(
                    str(source),
                    job.options,
                    location,
                )
                dependencies.extend(extra)
            else:
                content, extra = await self.registry.transform(
                    job.kind,
                    job.options,
                    location,
                )
                value = content
                dependencies.extend(extra)
            for dependency in dependencies:
                self.graph.add_dependency(node, dependency)
            patch = FrontMatterSetPatch(
                path=relative,
                pointer=target_pointer,
                value=value,
            )
            return _Rendered(
                job.path,
                job.identifier,
                None,
                patch,
                tuple(dict.fromkeys(dependencies)),
            )

        content, extra = await self.registry.transform(
            job.kind,
            job.options,
            location,
        )
        dependencies.extend(extra)
        for dependency in dependencies:
            self.graph.add_dependency(node, dependency)
        return _Rendered(
            job.path,
            job.identifier,
            content,
            None,
            tuple(dict.fromkeys(dependencies)),
        )

    def _register_builtins(self) -> None:
        async def sql_resource(
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[JsonValue, tuple[str, ...]]:
            rows, compiled, _params = self._query(options, location)
            dependencies = ["index:documents"]
            dependencies.extend(
                f"fs:file:{item.relative_to(self.root).as_posix()}"
                for item in compiled.files
            )
            return cast("JsonValue", rows), tuple(dependencies)

        async def jinja_renderer(
            value: JsonValue,
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[str, tuple[str, ...]]:
            template = str(options.get("template") or "@insitu/table")
            raw_params = options.get("params", {})
            if not isinstance(raw_params, dict):
                raise TypeError("renderer params must be a table")
            params = cast("dict[str, JsonValue]", raw_params)
            rows = _value_rows(value)
            content = self.renderer.render(
                template,
                rows=rows,
                params=params,
                location=location,
                extra={"value": value},
            )
            dependencies: list[str] = []
            if not template.startswith("@insitu/"):
                dependencies.append(f"fs:file:{self.config.paths.templates}/{template}")
            return content, tuple(dependencies)

        async def materialize(
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[str, tuple[str, ...]]:
            source = str(options.get("source") or "")
            renderer = str(options.get("renderer") or "jinja")
            if not source:
                raise ValueError("materialize transform requires source")
            value, dependencies = await self.registry.resource(
                source,
                options,
                location,
            )
            content, extra = await self.registry.render(
                renderer,
                value,
                options,
                location,
            )
            return content, (*dependencies, *extra)

        async def sql(
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[str, tuple[str, ...]]:
            merged = dict(options)
            merged["source"] = "sql"
            merged["renderer"] = "jinja"
            return await materialize(merged, location)

        async def tree(
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[str, tuple[str, ...]]:
            return render_tree(
                self.root,
                location,
                options,
                policy=self.index.policy,
            )

        async def include(
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[str, tuple[str, ...]]:
            return render_include(self.root, location, options)

        self.registry.register(
            CapabilitySpec(kind=CapabilityKind.RESOURCE, name="sql"),
            sql_resource,
        )
        self.registry.register(
            CapabilitySpec(kind=CapabilityKind.RENDERER, name="jinja"),
            jinja_renderer,
        )
        for name, handler in (
            ("materialize", materialize),
            ("sql", sql),
            ("tree", tree),
            ("include", include),
        ):
            self.registry.register(
                CapabilitySpec(kind=CapabilityKind.TRANSFORM, name=name),
                handler,
            )

    def _query(
        self,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[list[dict[str, object]], CompiledSql, dict[str, JsonValue]]:
        model = str(options.get("model") or "")
        if not model:
            raise ValueError("SQL materialization requires model")
        raw_params = options.get("params", {})
        if not isinstance(raw_params, dict):
            raise TypeError("SQL params must be a table")
        params = cast("dict[str, JsonValue]", raw_params)
        rows, compiled = self.models.execute(
            model,
            index=self.index,
            location=location,
            params=params,
        )
        return rows, compiled, params

    async def _resource(
        self,
        resource: str,
        location: Location,
        trace: tuple[str, ...] = (),
    ) -> ResourceRead:
        """Resolve a narrow host resource and flatten its dependencies."""
        if resource in trace:
            raise DependencyCycleError((*trace, resource))
        current = (*trace, resource)
        with trace_scope(current):
            if resource.startswith("git://"):
                return ResourceRead(
                    value=git_resource(self.git, resource),
                    dependencies=(resource,),
                )
            if resource.startswith("fs:file:"):
                raw = resource.removeprefix("fs:file:")
                path = (self.root / raw).resolve()
                if not path.is_relative_to(self.root):
                    raise ValueError(f"resource escapes project root: {resource}")
                text = path.read_text(encoding="utf-8")
                if path.suffix.lower() == ".md":
                    text = source_text(text)
                return ResourceRead(value=text, dependencies=(resource,))
            if resource.startswith("fs:glob:"):
                pattern = resource.removeprefix("fs:glob:")
                include = GitIgnoreSpec.from_lines([pattern])
                exclude = GitIgnoreSpec.from_lines(self.config.sources.exclude)
                matches = sorted(
                    path.relative_to(self.root).as_posix()
                    for path in self.root.rglob("*")
                    if path.is_file()
                    and include.match_file(path.relative_to(self.root).as_posix())
                    and not exclude.match_file(path.relative_to(self.root).as_posix())
                )
                return ResourceRead(
                    value=cast("JsonValue", matches),
                    dependencies=(resource,),
                )
            if resource.startswith("sql:model:"):
                split = urlsplit(resource.removeprefix("sql:model:"))
                params = cast(
                    "dict[str, JsonValue]",
                    {key: value for key, value in parse_qsl(split.query)},
                )
                rows, compiled = self.models.execute(
                    split.path,
                    index=self.index,
                    location=location,
                    params=params,
                )
                dependencies = [resource, "index:documents"]
                dependencies.extend(
                    f"fs:file:{item.relative_to(self.root).as_posix()}"
                    for item in compiled.files
                )
                return ResourceRead(
                    value=cast("JsonValue", rows),
                    dependencies=tuple(dict.fromkeys(dependencies)),
                )
            if resource.startswith("resource:"):
                split = urlsplit(resource.removeprefix("resource:"))
                options = cast(
                    "dict[str, JsonValue]",
                    {key: value for key, value in parse_qsl(split.query)},
                )
                value, dependencies = await self.registry.resource(
                    split.path,
                    options,
                    location,
                )
                return ResourceRead(
                    value=value,
                    dependencies=tuple(dict.fromkeys((resource, *dependencies))),
                )
        raise KeyError(f"unsupported host resource: {resource}")

    def _markdown_files(self) -> tuple[Path, ...]:
        return self.index.document_paths()


def _target(options: dict[str, JsonValue]) -> tuple[str, str | None]:
    target = options.get("target")
    if isinstance(target, str):
        if target.startswith("frontmatter:"):
            return "frontmatter", target.removeprefix("frontmatter:")
        return target, None
    if isinstance(target, dict):
        pointer = target.get("pointer")
        return str(target.get("kind", "markdown")), str(pointer) if pointer else None
    return "markdown", None


def _target_conflicts(root: Path, jobs: Iterable[_Job]) -> set[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for job in jobs:
        relative = job.path.relative_to(root).as_posix()
        kind, pointer = _target(job.options)
        target = pointer if kind == "frontmatter" else f"region:{job.identifier}"
        targets.append((relative, target or f"region:{job.identifier}"))
    conflicts: set[tuple[str, str]] = set()
    for index, (path, left) in enumerate(targets):
        for right_path, right in targets[index + 1 :]:
            if path != right_path:
                continue
            if left == right or (
                not left.startswith("region:")
                and not right.startswith("region:")
                and (
                    left.rstrip("/").startswith(f"{right.rstrip('/')}/")
                    or right.rstrip("/").startswith(f"{left.rstrip('/')}/")
                )
            ):
                conflicts.add((path, f"{left} / {right}"))
    return conflicts


def _dependencies_changed(
    dependencies: Iterable[str],
    *,
    raw: set[str],
    source: set[str],
    git: GitRepository | None = None,
) -> bool:
    for dependency in dependencies:
        if dependency.startswith("git://") and git is not None:
            if git.dependency_changed(dependency, raw):
                return True
        if dependency == "index:documents" and source:
            return True
        if dependency.startswith("fs:file:"):
            if dependency.removeprefix("fs:file:") in raw:
                return True
        if dependency.startswith("fs:glob:"):
            pattern = dependency.removeprefix("fs:glob:")
            matcher = GitIgnoreSpec.from_lines([pattern])
            if any(matcher.match_file(path) for path in raw):
                return True
        if dependency.startswith("fs:dir:"):
            directory = dependency.removeprefix("fs:dir:").strip("./")
            if any(
                not directory or path == directory or path.startswith(f"{directory}/")
                for path in raw
            ):
                return True
    return False


def _job_resources(root: Path, job: _Job) -> tuple[str, str, str]:
    relative = job.path.relative_to(root).as_posix()
    target_kind, pointer = _target(job.options)
    node = f"materialization:{relative}#{job.identifier}"
    output = (
        f"yaml:{relative}#{pointer}"
        if target_kind == "frontmatter"
        else f"markdown:{relative}#region:{job.identifier}"
    )
    return node, output, f"fs:file:{relative}"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _file_hash(path: Path) -> str:
    return _text_hash(path.read_text(encoding="utf-8"))


def _value_rows(value: JsonValue) -> list[dict[str, object]]:
    """Normalize a JSON value for table-shaped Jinja compatibility."""
    if isinstance(value, list):
        return [
            cast("dict[str, object]", item)
            if isinstance(item, dict)
            else {"value": item}
            for item in value
        ]
    if isinstance(value, dict):
        return [cast("dict[str, object]", value)]
    return [{"value": value}]


def _rows_value(rows: list[dict[str, object]]) -> JsonValue:
    """Preserve SQL row shape: one row is an object, many rows an array."""
    return cast("JsonValue", rows[0] if len(rows) == 1 else rows)


def _report(
    scanned: int,
    materializations: int,
    *,
    changed: Iterable[str] = (),
    diagnostics: Iterable[Diagnostic] = (),
    started: float,
) -> ReconcileReport:
    return ReconcileReport(
        scanned=scanned,
        materializations=materializations,
        changed=tuple(sorted(changed)),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.path or "", item.line or 0, item.message),
            )
        ),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
