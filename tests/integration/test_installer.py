"""Extension installation and dependency tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from insitu.graph import DependencyCycleError
from insitu.installer import ExtensionInstaller

pytestmark = pytest.mark.integration


def test_listing_extensions_does_not_create_state(project: Path) -> None:
    """A read-only extension listing must not materialize ``.insitu``."""
    assert ExtensionInstaller(project).list() == ()
    assert not (project / ".insitu").exists()


def _extension(
    path: Path,
    name: str,
    dependency: tuple[str, str] | None = None,
) -> None:
    path.mkdir(parents=True)
    manifest = [
        f'name = "{name}"',
        'version = "1.0.0"',
        "protocol = 1",
        "",
        "[extension]",
        'command = ["python", "extension.py"]',
    ]
    if dependency:
        manifest.extend(
            [
                "",
                "[[dependencies]]",
                f'name = "{dependency[0]}"',
                'version = ">=1"',
                f'source = "{dependency[1]}"',
            ]
        )
    (path / "insitu-extension.toml").write_text(
        "\n".join(manifest) + "\n",
        encoding="utf-8",
    )
    (path / "extension.py").write_text(
        "print('not started during install')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "feat: extension",
        ],
        check=True,
    )


def test_local_git_dependency_is_locked(project: Path, tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    parent = tmp_path / "parent"
    _extension(dependency, "test/dependency")
    _extension(parent, "test/parent", ("test/dependency", str(dependency)))
    installed = ExtensionInstaller(project).add(str(parent))
    assert installed.manifest.name == "test/parent"
    lock = (project / "insitu.lock").read_text(encoding="utf-8")
    assert "test/dependency" in lock and "test/parent" in lock


def test_dependency_cycle_fails(project: Path, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _extension(first, "test/first", ("test/second", str(second)))
    _extension(second, "test/second", ("test/first", str(first)))
    with pytest.raises(DependencyCycleError):
        ExtensionInstaller(project).add(str(first))


def test_installed_content_is_verified(project: Path, tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    _extension(extension, "test/verified")
    installed = ExtensionInstaller(project).add(str(extension))
    (installed.path / "extension.py").write_text(
        "print('tampered')\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="content hash changed"):
        ExtensionInstaller(project)


def test_failed_capability_install_rolls_back(project: Path, tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    _extension(extension, "test/missing-capability")
    manifest = extension / "insitu-extension.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "\n[[dependencies]]\n"
        + 'name = "missing.resource"\n'
        + "capability = true\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(extension), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(extension),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fix: missing capability",
        ],
        check=True,
    )
    before = (project / "insitu.toml").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="missing capability dependency"):
        ExtensionInstaller(project).add(str(extension))
    assert not tuple((project / ".insitu" / "extensions").iterdir())
    assert not (project / "insitu.lock").exists()
    assert (project / "insitu.toml").read_text(encoding="utf-8") == before
