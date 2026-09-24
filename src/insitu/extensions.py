"""Capability composition and external extension supervision."""

from __future__ import annotations

import asyncio
import os
import sys
import tomllib
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from fnmatch import fnmatch
from pathlib import Path
from typing import cast

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from insitu.config import ExtensionSource, InsituConfig
from insitu.graph import Dag, DependencyCycleError
from insitu.protocol import JsonRpcPeer, RpcProtocolError, read_stream_message
from insitu.types import (
    CapabilityKind,
    CapabilitySpec,
    CompositionMode,
    Diagnostic,
    ExtensionManifest,
    InitializeRequest,
    InitializeResult,
    InvocationRequest,
    InvocationResult,
    JsonValue,
    Location,
    ResourceRead,
)

Transform = Callable[
    [dict[str, JsonValue], Location],
    Awaitable[tuple[str, tuple[str, ...]]],
]
Resource = Callable[
    [dict[str, JsonValue], Location],
    Awaitable[tuple[JsonValue, tuple[str, ...]]],
]
Renderer = Callable[
    [JsonValue, dict[str, JsonValue], Location],
    Awaitable[tuple[str, tuple[str, ...]]],
]
Validator = Callable[
    [dict[str, JsonValue], Location],
    Awaitable[tuple[tuple[Diagnostic, ...], tuple[str, ...]]],
]
CapabilityHandler = Transform | Resource | Renderer | Validator
ResourceGetter = Callable[
    [str, Location, tuple[str, ...]],
    Awaitable[ResourceRead],
]

_BUILTIN_PACKAGES = {
    "insitu/include",
    "insitu/jinja",
    "insitu/sql",
    "insitu/tree",
}
_TRACE: ContextVar[tuple[str, ...]] = ContextVar("insitu_trace", default=())


@contextmanager
def trace_scope(trace: tuple[str, ...]) -> Iterator[None]:
    """Expose a resource trace to nested process invocations."""
    token: Token[tuple[str, ...]] = _TRACE.set(trace)
    try:
        yield
    finally:
        _TRACE.reset(token)


class CapabilityConflictError(ValueError):
    """Raised when capability composition is ambiguous."""


class CapabilityRegistry:
    """Typed registry for explicit extension composition."""

    def __init__(self) -> None:
        self._transforms: dict[str, Transform] = {}
        self._resources: dict[str, Resource] = {}
        self._renderers: dict[str, Renderer] = {}
        self._validators: dict[str, Validator] = {}
        self._contributors: dict[str, list[Transform]] = defaultdict(list)
        self._decorators: dict[
            str,
            dict[str, tuple[CapabilitySpec, Transform]],
        ] = defaultdict(dict)

    def has(self, name: str, kind: CapabilityKind | None = None) -> bool:
        """Return whether a named capability is available."""
        registries = {
            CapabilityKind.TRANSFORM: self._transforms,
            CapabilityKind.RESOURCE: self._resources,
            CapabilityKind.RENDERER: self._renderers,
            CapabilityKind.VALIDATOR: self._validators,
        }
        if kind is not None:
            return name in registries[kind]
        return any(name in registry for registry in registries.values())

    def register(
        self,
        spec: CapabilitySpec,
        handler: CapabilityHandler,
        *,
        allow_replace: bool = False,
    ) -> None:
        """Register a capability using its declared composition mode."""
        if spec.kind is CapabilityKind.TRANSFORM:
            self._register_transform(spec, cast("Transform", handler), allow_replace)
            return
        registry: dict[str, Resource | Renderer | Validator]
        if spec.kind is CapabilityKind.RESOURCE:
            registry = cast(
                "dict[str, Resource | Renderer | Validator]", self._resources
            )
        elif spec.kind is CapabilityKind.RENDERER:
            registry = cast(
                "dict[str, Resource | Renderer | Validator]", self._renderers
            )
        else:
            registry = cast(
                "dict[str, Resource | Renderer | Validator]", self._validators
            )
        if spec.mode in {CompositionMode.CONTRIBUTE, CompositionMode.DECORATE}:
            raise CapabilityConflictError(
                f"{spec.mode.value} is supported only for transforms"
            )
        name = spec.target if spec.mode is CompositionMode.REPLACE else spec.name
        if name is None:
            raise CapabilityConflictError("replacement capability requires a target")
        if spec.mode is CompositionMode.REPLACE:
            if name not in registry:
                raise CapabilityConflictError(
                    f"cannot replace missing capability: {name}"
                )
            if not allow_replace:
                raise CapabilityConflictError(
                    f"replacement requires project approval: {name}"
                )
        elif name in registry:
            raise CapabilityConflictError(f"capability already provided: {name}")
        registry[name] = cast("Resource | Renderer | Validator", handler)

    async def transform(
        self,
        name: str,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[str, tuple[str, ...]]:
        """Invoke a transform with ordered decorators and contributors."""
        handler = self._transforms.get(name)
        if handler is None:
            raise KeyError(f"unknown transform: {name}")
        content, dependencies = await handler(options, location)
        for _spec, decorator in self._ordered_decorators(name):
            decorated = dict(options)
            decorated["content"] = content
            content, extra = await decorator(decorated, location)
            dependencies += extra
        for contributor in self._contributors.get(name, ()):
            addition, extra = await contributor(options, location)
            content += addition
            dependencies += extra
        return content, tuple(dict.fromkeys(dependencies))

    async def resource(
        self,
        name: str,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[JsonValue, tuple[str, ...]]:
        """Resolve one typed resource provider."""
        handler = self._resources.get(name)
        if handler is None:
            raise KeyError(f"unknown resource: {name}")
        value, dependencies = await handler(options, location)
        return value, tuple(dict.fromkeys(dependencies))

    async def render(
        self,
        name: str,
        value: JsonValue,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[str, tuple[str, ...]]:
        """Render a typed value into a document fragment."""
        handler = self._renderers.get(name)
        if handler is None:
            raise KeyError(f"unknown renderer: {name}")
        content, dependencies = await handler(value, options, location)
        return content, tuple(dict.fromkeys(dependencies))

    async def validate(self, location: Location) -> tuple[Diagnostic, ...]:
        """Run all registered project validators concurrently."""
        results = await asyncio.gather(
            *(handler({}, location) for handler in self._validators.values())
        )
        return tuple(
            item for diagnostics, _dependencies in results for item in diagnostics
        )

    def validate_composition(self) -> None:
        """Reject unresolved decorator order references."""
        for target, values in self._decorators.items():
            names = set(values)
            for name, (spec, _handler) in values.items():
                missing = (set(spec.before) | set(spec.after)) - names
                if missing:
                    joined = ", ".join(sorted(missing))
                    raise CapabilityConflictError(
                        f"{target} decorator {name} references missing: {joined}"
                    )
            self._ordered_decorators(target)

    def _register_transform(
        self,
        spec: CapabilitySpec,
        handler: Transform,
        allow_replace: bool,
    ) -> None:
        if spec.mode is CompositionMode.CONTRIBUTE:
            if spec.name not in self._transforms:
                raise CapabilityConflictError(
                    f"cannot contribute to missing capability: {spec.name}"
                )
            self._contributors[spec.name].append(handler)
            return
        if spec.mode is CompositionMode.DECORATE:
            target = spec.target
            if target is None:
                raise CapabilityConflictError("decorator requires a target")
            if target not in self._transforms:
                raise CapabilityConflictError(
                    f"cannot decorate missing capability: {target}"
                )
            self._decorators[target][spec.name] = (spec, handler)
            self._ordered_decorators(target)
            return
        if spec.mode is CompositionMode.REPLACE:
            target = spec.target
            if target is None:
                raise CapabilityConflictError("replacement requires a target")
            if target not in self._transforms:
                raise CapabilityConflictError(
                    f"cannot replace missing capability: {target}"
                )
            if not allow_replace:
                raise CapabilityConflictError(
                    f"replacement requires project approval: {target}"
                )
            self._transforms[target] = handler
            return
        if spec.name in self._transforms:
            raise CapabilityConflictError(f"capability already provided: {spec.name}")
        self._transforms[spec.name] = handler

    def _ordered_decorators(
        self,
        target: str,
    ) -> tuple[tuple[CapabilitySpec, Transform], ...]:
        values = self._decorators.get(target, {})
        graph: Dag[str] = Dag()
        for name, (spec, _handler) in values.items():
            graph.add_node(name)
            for prior in spec.after:
                if prior in values:
                    graph.add_dependency(name, prior)
            for later in spec.before:
                if later in values:
                    graph.add_dependency(later, name)
        return tuple(values[name] for name in graph.order())


class ProcessExtension:
    """Long-lived process extension connected through JSON-RPC."""

    def __init__(
        self,
        project_root: Path,
        package_root: Path,
        manifest: ExtensionManifest,
    ) -> None:
        self.root = project_root
        self.package_root = package_root
        self.manifest = manifest
        self.process: asyncio.subprocess.Process | None = None
        self.peer: JsonRpcPeer | None = None

    async def start(self, resource_getter: ResourceGetter) -> None:
        """Spawn, initialize, and verify the extension process."""
        command = tuple(
            sys.executable if item == "{python}" else item
            for item in self.manifest.extension.command
        )
        cwd = (self.package_root / self.manifest.extension.cwd).resolve()
        if not cwd.is_relative_to(self.package_root.resolve()):
            raise ValueError(f"extension cwd escapes package: {cwd}")
        environment = os.environ.copy()
        python_path = os.pathsep.join(
            dict.fromkeys(
                str(Path(item).resolve())
                for item in sys.path
                if item and Path(item).exists()
            )
        )
        environment.update(
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=python_path,
            PYTHONUNBUFFERED="1",
        )
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
        )
        stdin = self.process.stdin
        stdout = self.process.stdout
        if stdin is None or stdout is None:
            raise RuntimeError("extension process pipes were not created")

        async def read() -> bytes:
            return await read_stream_message(stdout)

        async def write(data: bytes) -> None:
            stdin.write(data)
            await stdin.drain()

        self.peer = JsonRpcPeer(read, write)

        async def get_resource(raw: JsonValue | None) -> JsonValue:
            params = cast("dict[str, JsonValue]", raw or {})
            resource = str(params["resource"])
            if not _resource_allowed(resource, self.manifest.permissions):
                raise PermissionError(
                    f"{self.manifest.name} may not read resource {resource}"
                )
            trace = tuple(
                str(item) for item in cast("list[JsonValue]", params.get("trace", []))
            )
            result = await resource_getter(
                resource,
                Location.model_validate(params["location"]),
                trace,
            )
            return cast("JsonValue", result.model_dump(mode="json"))

        self.peer.handle("host.resource.get", get_resource)
        await self.peer.start()
        raw = await self.peer.request(
            "insitu.initialize",
            cast(
                "JsonValue",
                InitializeRequest(
                    root=self.root.as_posix(),
                    extension=self.manifest.name,
                ).model_dump(mode="json"),
            ),
        )
        result = InitializeResult.model_validate(raw)
        if result.name != self.manifest.name or result.version != self.manifest.version:
            raise RuntimeError(
                f"manifest/runtime identity mismatch for {self.manifest.name}"
            )
        for spec in self.manifest.capabilities:
            raw_kind = result.capabilities.get(spec.kind.value)
            if not isinstance(raw_kind, dict) or spec.name not in raw_kind:
                raise RuntimeError(
                    f"{self.manifest.name} did not register "
                    f"{spec.kind.value}:{spec.name}"
                )
            raw_capability = raw_kind[spec.name]
            if not isinstance(raw_capability, dict):
                raise RuntimeError(
                    f"{self.manifest.name} returned invalid schemas for "
                    f"{spec.kind.value}:{spec.name}"
                )
            for key, declared in (
                ("params", spec.params_schema),
                ("value", spec.value_schema),
                ("result", spec.result_schema),
            ):
                if declared is not None and raw_capability.get(key) != declared:
                    raise RuntimeError(
                        f"{self.manifest.name} runtime {key} schema mismatch "
                        f"for {spec.kind.value}:{spec.name}"
                    )

    async def transform(
        self,
        name: str,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[str, tuple[str, ...]]:
        """Invoke an advertised transform."""
        result = await self._invoke("insitu.transform", name, options, location)
        return result.content or "", result.dependencies

    async def resource(
        self,
        name: str,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[JsonValue, tuple[str, ...]]:
        """Invoke an advertised resource provider."""
        result = await self._invoke("insitu.resource", name, options, location)
        return result.value, result.dependencies

    async def render(
        self,
        name: str,
        value: JsonValue,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[str, tuple[str, ...]]:
        """Invoke an advertised renderer."""
        result = await self._invoke(
            "insitu.render",
            name,
            options,
            location,
            value=value,
        )
        return result.content or "", result.dependencies

    async def validate(
        self,
        name: str,
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[tuple[Diagnostic, ...], tuple[str, ...]]:
        """Invoke an advertised validator."""
        result = await self._invoke("insitu.validate", name, options, location)
        return result.diagnostics, result.dependencies

    async def close(self) -> None:
        """Shut down the peer and child process."""
        if self.peer is not None:
            try:
                await self.peer.request("insitu.shutdown", timeout=2)
            except (RpcProtocolError, TimeoutError, RuntimeError):
                pass
            await self.peer.close()
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
                await self.process.stdin.wait_closed()
            if self.process.returncode is None:
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def _invoke(
        self,
        method: str,
        name: str,
        options: dict[str, JsonValue],
        location: Location,
        *,
        value: JsonValue | None = None,
    ) -> InvocationResult:
        if self.peer is None:
            raise RuntimeError(f"extension is not running: {self.manifest.name}")
        trace = _TRACE.get()
        node = f"{method.removeprefix('insitu.')}:{name}"
        if not trace or trace[-1] != node:
            if node in trace:
                raise DependencyCycleError((*trace, node))
            trace = (*trace, node)
        request = InvocationRequest(
            name=name,
            options=options,
            location=location,
            value=value,
            trace=trace,
        )
        raw = await self.peer.request(
            method,
            cast("JsonValue", request.model_dump(mode="json")),
        )
        return InvocationResult.model_validate(raw)


class ExtensionManager:
    """Load configured process extensions into a capability registry."""

    def __init__(
        self,
        root: Path,
        config: InsituConfig,
        registry: CapabilityRegistry,
    ) -> None:
        self.root = root
        self.config = config
        self.registry = registry
        self.processes: list[ProcessExtension] = []

    async def start(self, resource_getter: ResourceGetter) -> None:
        """Start enabled process extensions in dependency order."""
        paths, manifests, settings = self._discover()
        graph: Dag[str] = Dag()
        providers = _capability_providers(manifests)
        for name, manifest in manifests.items():
            graph.add_node(name)
            for dependency in manifest.dependencies:
                if not dependency.capability:
                    if dependency.name in _BUILTIN_PACKAGES:
                        continue
                    target = manifests.get(dependency.name)
                    if target is None:
                        raise ValueError(
                            f"missing extension dependency: {name} -> {dependency.name}"
                        )
                    _check_version(target, dependency.version)
                    graph.add_dependency(name, dependency.name)
                    continue
                kind, capability = _capability_name(dependency.name)
                if self.registry.has(capability, kind):
                    continue
                candidates = providers.get((kind, capability), set())
                if kind is None:
                    candidates = set().union(
                        *(
                            providers.get((item, capability), set())
                            for item in CapabilityKind
                        )
                    )
                if len(candidates) != 1:
                    raise ValueError(
                        f"capability dependency {name} -> {dependency.name} "
                        f"has {len(candidates)} providers"
                    )
                graph.add_dependency(name, next(iter(candidates)))
        try:
            for name in graph.order():
                manifest = manifests[name]
                for dependency in manifest.dependencies:
                    if not dependency.capability:
                        continue
                    kind, capability = _capability_name(dependency.name)
                    if not self.registry.has(capability, kind):
                        missing = f"{name} -> {dependency.name}"
                        raise ValueError(f"missing capability dependency: {missing}")
                process = ProcessExtension(self.root, paths[name], manifest)
                await process.start(resource_getter)
                self.processes.append(process)
                setting = settings.get(name)
                approval = set(setting.allow_replace if setting else ())
                for spec in manifest.capabilities:
                    self.registry.register(
                        spec,
                        _process_handler(process, spec),
                        allow_replace=(spec.target or spec.name) in approval,
                    )
            self.registry.validate_composition()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Stop all process extensions."""
        await asyncio.gather(
            *(process.close() for process in self.processes),
            return_exceptions=True,
        )
        self.processes.clear()

    def _discover(
        self,
    ) -> tuple[
        dict[str, Path],
        dict[str, ExtensionManifest],
        dict[str, ExtensionSource],
    ]:
        paths: dict[str, Path] = {}
        manifests: dict[str, ExtensionManifest] = {}
        settings: dict[str, ExtensionSource] = {}
        candidates: list[tuple[Path, ExtensionSource | None]] = []
        for source in self.config.extensions:
            if not source.enabled:
                continue
            if source.source is not None:
                raise ValueError(
                    "remote extensions must be installed with `insitu extension add`"
                )
            if source.path is not None:
                candidates.append(((self.root / source.path).resolve(), source))
        installed = self.root / ".insitu" / "extensions"
        if installed.exists():
            candidates.extend(
                (path, None) for path in installed.iterdir() if path.is_dir()
            )
        for path, source in candidates:
            manifest_path = path / "insitu-extension.toml"
            if not manifest_path.exists():
                continue
            manifest = load_manifest(manifest_path)
            previous = paths.get(manifest.name)
            if previous is not None and previous != path:
                raise ValueError(f"extension configured twice: {manifest.name}")
            paths[manifest.name] = path
            manifests[manifest.name] = manifest
            if source is not None:
                settings[manifest.name] = source
        return paths, manifests, settings


def load_manifest(path: Path) -> ExtensionManifest:
    """Load and validate an extension manifest."""
    with path.open("rb") as stream:
        return ExtensionManifest.model_validate(tomllib.load(stream))


def _process_handler(
    process: ProcessExtension,
    spec: CapabilitySpec,
) -> CapabilityHandler:
    if spec.kind is CapabilityKind.TRANSFORM:

        async def transform(
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[str, tuple[str, ...]]:
            return await process.transform(spec.name, options, location)

        return transform
    if spec.kind is CapabilityKind.RESOURCE:

        async def resource(
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[JsonValue, tuple[str, ...]]:
            return await process.resource(spec.name, options, location)

        return resource
    if spec.kind is CapabilityKind.RENDERER:

        async def renderer(
            value: JsonValue,
            options: dict[str, JsonValue],
            location: Location,
        ) -> tuple[str, tuple[str, ...]]:
            return await process.render(spec.name, value, options, location)

        return renderer

    async def validator(
        options: dict[str, JsonValue],
        location: Location,
    ) -> tuple[tuple[Diagnostic, ...], tuple[str, ...]]:
        return await process.validate(spec.name, options, location)

    return validator


def _capability_name(name: str) -> tuple[CapabilityKind | None, str]:
    prefix, separator, value = name.partition(":")
    try:
        kind = CapabilityKind(prefix)
    except ValueError:
        return None, name
    return (kind, value) if separator and value else (None, name)


def _capability_providers(
    manifests: dict[str, ExtensionManifest],
) -> dict[tuple[CapabilityKind | None, str], set[str]]:
    providers: dict[tuple[CapabilityKind | None, str], set[str]] = defaultdict(set)
    for package, manifest in manifests.items():
        for spec in manifest.capabilities:
            name = spec.target if spec.mode is CompositionMode.REPLACE else spec.name
            if name is None:
                raise CapabilityConflictError(
                    "replacement capability requires a target"
                )
            providers[(spec.kind, name)].add(package)
    return providers


def _check_version(manifest: ExtensionManifest, constraint: str) -> None:
    if constraint != "*" and Version(manifest.version) not in SpecifierSet(constraint):
        raise ValueError(
            f"{manifest.name} {manifest.version} does not satisfy {constraint}"
        )


def _resource_allowed(resource: str, permissions: tuple[str, ...]) -> bool:
    """Match one resource read against manifest permission patterns."""
    prefixes = {
        "fs:file:": "fs:read:",
        "fs:glob:": "fs:read:",
        "git://": "git:read:",
        "sql:model:": "sql:read:",
        "resource:": "resource:read:",
    }
    for prefix, permission_prefix in prefixes.items():
        if not resource.startswith(prefix):
            continue
        value = resource.removeprefix(prefix).partition("?")[0]
        return any(
            permission.startswith(permission_prefix)
            and _matches(value, permission.removeprefix(permission_prefix))
            for permission in permissions
        )
    return False


def _matches(value: str, pattern: str) -> bool:
    return fnmatch(value, pattern) or (
        pattern.startswith("**/") and fnmatch(value, pattern.removeprefix("**/"))
    )
