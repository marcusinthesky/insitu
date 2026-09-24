"""Git/local extension installation with deterministic lock records."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import override

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from insitu.extensions import load_manifest
from insitu.graph import Dag
from insitu.types import (
    CapabilityKind,
    CompositionMode,
    ExtensionManifest,
    LockedExtension,
)

_BUILTIN_PACKAGES = {
    "include",
    "insitu/include",
    "insitu/jinja",
    "insitu/sql",
    "insitu/tree",
    "jinja",
    "sql",
    "tree",
}
_BUILTIN_CAPABILITIES = {
    "renderer:jinja",
    "resource:sql",
    "transform:include",
    "transform:materialize",
    "transform:sql",
    "transform:tree",
}
_IGNORED = {".git", ".insitu", "__pycache__", "node_modules", "target"}


@dataclass(frozen=True, slots=True)
class InstalledExtension:
    """Installed manifest and its project-relative directory."""

    manifest: ExtensionManifest
    path: Path
    lock: LockedExtension


class ExtensionInstaller:
    """Resolve local or Git-hosted extensions into ``.insitu/extensions``."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.destination = self.root / ".insitu" / "extensions"
        self._installed: dict[str, InstalledExtension] = {}
        self._graph: Dag[str] = Dag()
        self._visiting: set[str] = set()
        self._load_existing()

    def list(self) -> tuple[InstalledExtension, ...]:
        """Return installed extensions in dependency-safe order."""
        return tuple(
            self._installed[name]
            for name in self._graph.order()
            if name in self._installed
        )

    def add(self, source: str, *, version: str | None = None) -> InstalledExtension:
        """Install ``source`` and all source-addressable dependencies."""
        self.destination.mkdir(parents=True, exist_ok=True)
        baseline = set(self._installed)
        baseline_paths = {item.path.resolve() for item in self._installed.values()}
        snapshots = {
            path: path.read_bytes() if path.is_file() else None
            for path in (self.root / "insitu.lock", self.root / "insitu.toml")
        }
        try:
            installed = self._install(source, version=version, parent=None)
            self._validate_capabilities()
            self._write_lock()
            for item in self.list():
                self._ensure_config_path(item.path)
        except BaseException:
            for path in self.destination.iterdir():
                if path.resolve() in baseline_paths:
                    continue
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            self._installed = {
                name: item for name, item in self._installed.items() if name in baseline
            }
            self._rebuild_graph()
            for path, content in snapshots.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            raise
        return installed

    def _install(
        self,
        source: str,
        *,
        version: str | None,
        parent: str | None,
    ) -> InstalledExtension:
        with self._materialize(source) as checkout:
            manifest = load_manifest(checkout / "insitu-extension.toml")
            if parent:
                self._graph.add_dependency(parent, manifest.name)
            else:
                self._graph.add_node(manifest.name)
            if existing := self._installed.get(manifest.name):
                _check_version(existing.manifest, version)
                return existing
            if manifest.name in self._visiting:
                raise ValueError(f"extension dependency cycle includes {manifest.name}")
            _check_version(manifest, version)
            self._visiting.add(manifest.name)
            try:
                for dependency in manifest.dependencies:
                    constraint = (
                        None if dependency.version == "*" else dependency.version
                    )
                    if dependency.capability:
                        if dependency.source:
                            self._install(
                                _relative_source(checkout, dependency.source),
                                version=constraint,
                                parent=manifest.name,
                            )
                        continue
                    self._graph.add_dependency(manifest.name, dependency.name)
                    current = self._installed.get(dependency.name)
                    if current is not None:
                        _check_version(current.manifest, constraint)
                        continue
                    if dependency.name in _BUILTIN_PACKAGES:
                        continue
                    if dependency.source is None:
                        raise ValueError(f"dependency {dependency.name} needs a source")
                    self._install(
                        _relative_source(checkout, dependency.source),
                        version=constraint,
                        parent=manifest.name,
                    )
                target = self.destination / _slug(manifest.name)
                staging = target.with_name(f".{target.name}.tmp")
                if staging.exists():
                    shutil.rmtree(staging)
                shutil.copytree(
                    checkout,
                    staging,
                    ignore=shutil.ignore_patterns(*sorted(_IGNORED)),
                )
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
                installed = InstalledExtension(
                    manifest=manifest,
                    path=target,
                    lock=LockedExtension(
                        name=manifest.name,
                        version=manifest.version,
                        source=source,
                        revision=_git_revision(checkout),
                        sha256=_tree_hash(target),
                        dependencies=tuple(
                            item.name
                            for item in manifest.dependencies
                            if not item.capability
                        ),
                        permissions=manifest.permissions,
                    ),
                )
                self._installed[manifest.name] = installed
                return installed
            finally:
                self._visiting.discard(manifest.name)

    def _load_existing(self) -> None:
        records: dict[str, LockedExtension] = {}
        lock_path = self.root / "insitu.lock"
        if lock_path.is_file():
            with lock_path.open("rb") as stream:
                for raw in tomllib.load(stream).get("extensions", []):
                    item = LockedExtension.model_validate(raw)
                    records[item.name] = item
        directories = self.destination.iterdir() if self.destination.exists() else ()
        for directory in sorted(path for path in directories if path.is_dir()):
            manifest_path = directory / "insitu-extension.toml"
            if not manifest_path.is_file():
                continue
            manifest = load_manifest(manifest_path)
            lock = records.get(manifest.name)
            if lock is None:
                raise ValueError(f"installed extension is not locked: {manifest.name}")
            if lock.version != manifest.version or lock.protocol != manifest.protocol:
                raise ValueError(f"extension lock metadata changed: {manifest.name}")
            if lock.sha256 != _tree_hash(directory):
                raise ValueError(f"extension content hash changed: {manifest.name}")
            self._installed[manifest.name] = InstalledExtension(
                manifest,
                directory,
                lock,
            )
            self._graph.add_node(manifest.name)
        unknown = set(records) - set(self._installed)
        if unknown:
            missing = ", ".join(sorted(unknown))
            raise ValueError(f"locked extension is missing: {missing}")
        self._rebuild_graph()

    def _rebuild_graph(self) -> None:
        """Recreate package dependency state from installed manifests."""
        self._graph = Dag()
        for name in self._installed:
            self._graph.add_node(name)
        for item in self._installed.values():
            for dependency in item.manifest.dependencies:
                if not dependency.capability and dependency.name in self._installed:
                    self._graph.add_dependency(item.manifest.name, dependency.name)

    def _materialize(self, source: str) -> _ExistingDirectory | _TemporaryCheckout:
        path = Path(source).expanduser()
        if path.exists():
            return _ExistingDirectory(path.resolve())
        temporary = tempfile.TemporaryDirectory(prefix="insitu-extension-")
        checkout = Path(temporary.name)
        url, revision = _git_source(source)
        try:
            subprocess.run(  # noqa: S603
                ["git", "clone", "--quiet", url, str(checkout)],
                check=True,
            )
            if revision:
                subprocess.run(  # noqa: S603
                    ["git", "-C", str(checkout), "checkout", "--quiet", revision],
                    check=True,
                )
        except BaseException:
            temporary.cleanup()
            raise
        return _TemporaryCheckout(temporary, checkout)

    def _validate_capabilities(self) -> None:
        available = set(_BUILTIN_CAPABILITIES)
        for item in self._installed.values():
            for spec in item.manifest.capabilities:
                name = (
                    spec.target if spec.mode is CompositionMode.REPLACE else spec.name
                )
                if name is None:
                    raise ValueError("replacement capability requires a target")
                available.add(f"{spec.kind.value}:{name}")
        for item in self._installed.values():
            for dependency in item.manifest.dependencies:
                if not dependency.capability:
                    continue
                if ":" in dependency.name:
                    found = dependency.name in available
                else:
                    found = any(
                        f"{kind.value}:{dependency.name}" in available
                        for kind in CapabilityKind
                    )
                if not found:
                    raise ValueError(
                        f"missing capability dependency: "
                        f"{item.manifest.name} -> {dependency.name}"
                    )

    def _write_lock(self) -> None:
        lines = ["version = 1", ""]
        for item in sorted(
            self._installed.values(),
            key=lambda value: value.manifest.name,
        ):
            lock = item.lock
            lines.extend(
                [
                    "[[extensions]]",
                    f"name = {_toml(lock.name)}",
                    f"version = {_toml(lock.version)}",
                    f"source = {_toml(lock.source)}",
                    *([f"revision = {_toml(lock.revision)}"] if lock.revision else []),
                    f"sha256 = {_toml(lock.sha256)}",
                    f"protocol = {lock.protocol}",
                    "dependencies = ["
                    + ", ".join(_toml(value) for value in lock.dependencies)
                    + "]",
                    "permissions = ["
                    + ", ".join(_toml(value) for value in lock.permissions)
                    + "]",
                    "",
                ]
            )
        (self.root / "insitu.lock").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )

    def _ensure_config_path(self, path: Path) -> None:
        config = self.root / "insitu.toml"
        relative = path.relative_to(self.root).as_posix()
        text = config.read_text(encoding="utf-8")
        marker = f'path = "{relative}"'
        if marker not in text:
            config.write_text(
                f"{text.rstrip()}\n\n[[extensions]]\n{marker}\n",
                encoding="utf-8",
            )


class _ExistingDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_args: object) -> None:
        return None


class _TemporaryCheckout(_ExistingDirectory):
    def __init__(
        self,
        temporary: tempfile.TemporaryDirectory[str],
        path: Path,
    ) -> None:
        super().__init__(path)
        self.temporary = temporary

    @override
    def __exit__(self, *_args: object) -> None:
        self.temporary.cleanup()


def _check_version(manifest: ExtensionManifest, constraint: str | None) -> None:
    if constraint and Version(manifest.version) not in SpecifierSet(constraint):
        raise ValueError(
            f"{manifest.name} {manifest.version} does not satisfy {constraint}"
        )


def _relative_source(root: Path, source: str) -> str:
    if source.startswith(("git:", "github:", "http:", "https:", "ssh:")):
        return source
    candidate = root / source
    return str(candidate) if candidate.exists() else source


def _git_source(source: str) -> tuple[str, str | None]:
    if source.startswith("github:"):
        value = source.removeprefix("github:")
        repository, separator, revision = value.partition("@")
        return f"https://github.com/{repository}.git", revision if separator else None
    if source.startswith("git:"):
        value = source.removeprefix("git:")
        url, separator, revision = value.rpartition("#")
        return (url, revision) if separator else (value, None)
    return source, None


def _git_revision(path: Path) -> str | None:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    files = (candidate for candidate in path.rglob("*") if candidate.is_file())
    for item in sorted(files):
        if any(part in _IGNORED for part in item.relative_to(path).parts):
            continue
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def _slug(name: str) -> str:
    return name.replace("/", "-").replace(".", "-")


def _toml(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
