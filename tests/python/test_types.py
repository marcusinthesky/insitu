import pytest
from insitu.sdk import Extension
from insitu.types import *
from pydantic import ValidationError


@pytest.mark.parametrize(
    "name,directory,depth",
    [("a.md", "", 0), ("docs/a.md", "docs", 1), ("docs/tasks/a.md", "docs/tasks", 2)],
)
def test_locations(name, directory, depth):
    location = Location.from_path(name)
    assert location.directory == directory and location.depth == depth


@pytest.mark.parametrize(
    "raw",
    [
        {"path": "a", "directory": "", "unknown": 1},
        {"path": "a", "directory": "", "depth": -1},
    ],
)
def test_location_boundaries(raw):
    with pytest.raises(ValidationError):
        Location.model_validate(raw)


@pytest.mark.parametrize("protocol", [0, 2, 255])
def test_protocol_version(protocol):
    with pytest.raises(ValidationError):
        InitializeRequest(protocol=protocol, root="/tmp", extension="test")


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "transform", "name": "x", "mode": "decorate"},
        {"kind": "transform", "name": "x", "mode": "replace"},
        {"kind": "transform", "name": "x", "target": "other"},
        {
            "kind": "transform",
            "name": "x",
            "mode": "decorate",
            "target": "other",
            "before": ["a"],
            "after": ["a"],
        },
    ],
)
def test_composition_validation(raw):
    with pytest.raises(ValidationError):
        CapabilitySpec.model_validate(raw)


def test_models_are_frozen():
    value = Location.from_path("a.md")
    with pytest.raises(ValidationError):
        value.path = "b.md"


def test_duplicate_registration():
    app = Extension(name="test", version="1")
    app.transform("x")(lambda p, c: "x")
    with pytest.raises(ValueError, match="duplicate"):
        app.transform("x")(lambda p, c: "x")


def test_report_semantics():
    assert ReconcileReport().ok
    assert not ReconcileReport(
        diagnostics=(Diagnostic(severity=Severity.ERROR, message="bad"),)
    ).ok


def test_diagnostic_line_must_be_positive():
    with pytest.raises(ValidationError):
        Diagnostic(severity=Severity.ERROR, message="bad", line=0)
