"""Capability composition tests."""

import pytest

from insitu.extensions import CapabilityConflictError, CapabilityRegistry
from insitu.types import (
    CapabilityKind,
    CapabilitySpec,
    CompositionMode,
    JsonValue,
    Location,
)

pytestmark = pytest.mark.unit


async def _base(
    _options: dict[str, JsonValue],
    _location: Location,
) -> tuple[str, tuple[str, ...]]:
    return "base", ()


async def _decorator(
    options: dict[str, JsonValue],
    _location: Location,
) -> tuple[str, tuple[str, ...]]:
    return f"[{options['content']}]", ()


async def test_decorator_is_explicit() -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilitySpec(kind=CapabilityKind.TRANSFORM, name="x"), _base)
    registry.register(
        CapabilitySpec(
            kind=CapabilityKind.TRANSFORM,
            name="brackets",
            mode=CompositionMode.DECORATE,
            target="x",
        ),
        _decorator,
    )
    result = await registry.transform("x", {}, Location.from_path("README.md"))
    assert result == ("[base]", ())
    with pytest.raises(CapabilityConflictError):
        registry.register(
            CapabilitySpec(kind=CapabilityKind.TRANSFORM, name="x"),
            _base,
        )
