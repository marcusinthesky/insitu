"""Validated v1 boundary types retained for Python process extensions."""

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
    model_config = ConfigDict(extra="forbid", frozen=True)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Location(FrozenModel):
    path: str
    directory: str
    root: str = "."
    depth: int = Field(default=0, ge=0)

    @classmethod
    def from_path(cls, path: str) -> Location:
        value = PurePosixPath(path)
        parent = "" if str(value.parent) == "." else value.parent.as_posix()
        return cls(
            path=value.as_posix(),
            directory=parent,
            depth=len(value.parent.parts) if parent else 0,
        )


class Diagnostic(FrozenModel):
    severity: Severity
    message: str
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    code: str | None = None


class CapabilityKind(StrEnum):
    RESOURCE = "resource"
    TRANSFORM = "transform"
    RENDERER = "renderer"
    VALIDATOR = "validator"


class CompositionMode(StrEnum):
    PROVIDE = "provide"
    CONTRIBUTE = "contribute"
    DECORATE = "decorate"
    REPLACE = "replace"


class CapabilitySpec(FrozenModel):
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
    name: str = Field(min_length=1)
    version: str = "*"
    source: str | None = None
    capability: bool = False


class ExtensionCommand(FrozenModel):
    command: tuple[str, ...]
    cwd: str = "."

    @field_validator("command")
    @classmethod
    def command_is_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("extension command cannot be empty")
        return value


class ExtensionManifest(FrozenModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    protocol: Literal[1] = 1
    extension: ExtensionCommand
    capabilities: tuple[CapabilitySpec, ...] = ()
    dependencies: tuple[ExtensionDependency, ...] = ()
    permissions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def entries_are_unique(self) -> ExtensionManifest:
        capabilities = [(item.kind, item.name) for item in self.capabilities]
        dependencies = [(item.capability, item.name) for item in self.dependencies]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("extension capabilities must be unique")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("extension dependencies must be unique")
        return self


class LockedExtension(FrozenModel):
    name: str
    version: str
    source: str
    revision: str | None = None
    sha256: str
    protocol: Literal[1] = 1
    dependencies: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


class FrontMatterSetPatch(FrozenModel):
    kind: Literal["frontmatter_set"] = "frontmatter_set"
    path: str
    pointer: str
    value: JsonValue


class FrontMatterDeletePatch(FrozenModel):
    kind: Literal["frontmatter_delete"] = "frontmatter_delete"
    path: str
    pointer: str


class ReconcileReport(FrozenModel):
    scanned: int = 0
    materializations: int = 0
    changed: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not any(item.severity is Severity.ERROR for item in self.diagnostics)


class ResourceRead(FrozenModel):
    value: JsonValue
    dependencies: tuple[str, ...] = ()


class InvocationRequest(FrozenModel):
    name: str
    options: dict[str, JsonValue] = Field(default_factory=dict)
    location: Location
    value: JsonValue | None = None
    trace: tuple[str, ...] = ()


class InvocationResult(FrozenModel):
    content: str | None = None
    value: JsonValue | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    dependencies: tuple[str, ...] = ()


class InitializeRequest(FrozenModel):
    protocol: Literal[1] = 1
    root: str
    extension: str


class InitializeResult(FrozenModel):
    name: str
    version: str
    protocol: Literal[1] = 1
    capabilities: dict[str, JsonValue] = Field(default_factory=dict)
