import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "release_gate", ROOT / "scripts/release_gate.py"
)
release_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_gate)


def test_unverified_source_is_blocked(tmp_path):
    errors = release_gate.problems(tmp_path, {}, "abc")
    assert "missing Cargo.lock" in errors
    assert "missing flake.lock" in errors
    assert "missing maintainer approval" in errors
    assert any("differential_compatibility" in error for error in errors)


def test_evidence_is_bound_to_commit(tmp_path):
    (tmp_path / "Cargo.lock").write_text("test-only")
    (tmp_path / "flake.lock").write_text("test-only")
    evidence = {
        "commit": "old",
        "approved_by": "test-maintainer",
        "checks": {name: "passed" for name in release_gate.REQUIRED},
    }
    assert release_gate.problems(tmp_path, evidence, "new") == [
        "release evidence does not match HEAD"
    ]


def test_explicit_approval_is_required_even_with_green_checks(tmp_path):
    (tmp_path / "Cargo.lock").touch()
    (tmp_path / "flake.lock").touch()
    evidence = {
        "commit": "abc",
        "checks": {name: "passed" for name in release_gate.REQUIRED},
    }
    assert release_gate.problems(tmp_path, evidence, "abc") == [
        "missing maintainer approval"
    ]
