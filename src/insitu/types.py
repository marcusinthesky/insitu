"""Validated boundary types shared by the host and extensions."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


class FrozenModel(BaseModel):
    """Immutable boundary model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Severity(StrEnum):
    """Diagnostic severity."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Location(FrozenModel):
    """Repository-relative context for one materialization."""

    path: str
    directory: str
    root: str = "."
    depth: int = Field(default=0, ge=0)

    @classmethod
    def from_path(cls, path: str) -> Location:
        """Create a location from a POSIX repository path."""
        value = PurePosixPath(path)
        parent = "" if str(value.parent) == "." else value.parent.as_posix()
        return cls(
            path=value.as_posix(),
            directory=parent,
            depth=len(value.parent.parts) if parent else 0,
        )


class Diagnostic(FrozenModel):
    """User-facing error, warning, or informational message."""

    severity: Severity
    message: str
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    code: str | None = None


class CapabilityKind(StrEnum):
    """Extension capability category."""

    RESOURCE = "resource"
    TRANSFORM = "transform"
    RENDERER = "renderer"
    VALIDATOR = "validator"


class CompositionMode(StrEnum):
    """How a capability composes with existing registrations."""

    PROVIDE = "provide"
    CONTRIBUTE = "contribute"
    DECORATE = "decorate"
    REPLACE = "replace"


class CapabilitySpec(FrozenModel):
    """A named capability advertised by an extension."""

    kind: CapabilityKind
    name: str = Field(min_length=1)
    mode: CompositionMode = CompositionMode.PROVIDE
    target: str | None = None
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    params_schema: dict[str, JsonValue] | None = None
    value_schema: dict[str, JsonValue] | None = None
    result_schema: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def composition_is_explicit(self) -> CapabilitySpec:
        """Require a target exactly when composition needs one."""
        targeted = self.mode in {CompositionMode.DECORATE, CompositionMode.REPLACE}
        if targeted and not self.target:
            raise ValueError(f"{self.mode.value} capability requires target")
        if not targeted and self.target:
            raise ValueError(f"{self.mode.value} capability cannot define target")
        if set(self.before) & set(self.after):
            raise ValueError(
                "a decorator cannot be both before and after the same item"
            )
        return self


class ExtensionDependency(FrozenModel):
    """Package or capability needed by an extension."""

    name: str = Field(min_length=1)
    version: str = "*"
    source: str | None = None
    capability: bool = False


class ExtensionCommand(FrozenModel):
    """Executable process entry point."""

    command: tuple[str, ...]
    cwd: str = "."

    @field_validator("command")
    @classmethod
    def command_is_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject empty executable commands."""
        if not value:
            raise ValueError("extension command cannot be empty")
        return value


class ExtensionManifest(FrozenModel):
    """Language-neutral extension manifest."""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    protocol: Literal[1] = 1
    extension: ExtensionCommand
    capabilities: tuple[CapabilitySpec, ...] = ()
    dependencies: tuple[ExtensionDependency, ...] = ()
    permissions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def entries_are_unique(self) -> ExtensionManifest:
        """Reject duplicate capability and dependency declarations."""
        capabilities = [(item.kind, item.name) for item in self.capabilities]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("extension capabilities must be unique")
        dependencies = [(item.capability, item.name) for item in self.dependencies]
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("extension dependencies must be unique")
        return self


class LockedExtension(FrozenModel):
    """Immutable extension resolution recorded in ``insitu.lock``."""

    name: str
    version: str
    source: str
    revision: str | None = None
    sha256: str
    protocol: Literal[1] = 1
    dependencies: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


class FrontMatterSetPatch(FrozenModel):
    """Set one JSON-Pointer-like path in YAML front matter."""

    kind: Literal["frontmatter_set"] = "frontmatter_set"
    path: str
    pointer: str
    value: JsonValue


class FrontMatterDeletePatch(FrozenModel):
    """Delete one JSON-Pointer-like path in YAML front matter."""

    kind: Literal["frontmatter_delete"] = "frontmatter_delete"
    path: str
    pointer: str


class ReconcileReport(FrozenModel):
    """Result of one reconciliation pass."""

    scanned: int = 0
    materializations: int = 0
    changed: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """Return whether no error diagnostics were produced."""
        return not any(item.severity is Severity.ERROR for item in self.diagnostics)


class ResourceRead(FrozenModel):
    """A resource value plus flattened transitive dependencies."""

    value: JsonValue
    dependencies: tuple[str, ...] = ()


class InvocationRequest(FrozenModel):
    """Typed request sent from the host to a process extension."""

    name: str
    options: dict[str, JsonValue] = Field(default_factory=dict)
    location: Location
    value: JsonValue | None = None
    trace: tuple[str, ...] = ()


class InvocationResult(FrozenModel):
    """Typed result returned by a process extension."""

    content: str | None = None
    value: JsonValue | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    dependencies: tuple[str, ...] = ()


class InitializeRequest(FrozenModel):
    """Host metadata supplied during protocol initialization."""

    protocol: Literal[1] = 1
    root: str
    extension: str


class InitializeResult(FrozenModel):
    """Extension identity and runtime-generated capability schemas."""

    name: str
    version: str
    protocol: Literal[1] = 1
    capabilities: dict[str, JsonValue] = Field(default_factory=dict)
