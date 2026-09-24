"""Polyglot process boundary tests using the Python SDK."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from insitu.reconcile import Reconciler
from insitu.types import ReconcileReport

pytestmark = [pytest.mark.integration, pytest.mark.protocol]


def _write_extension(
    project: Path,
    name: str,
    capabilities: tuple[tuple[str, str], ...],
    code: str,
    *,
    permissions: tuple[str, ...] = ("fs:read:**/*.md",),
) -> None:
    slug = name.rpartition("/")[2]
    extension = project / "tools" / slug
    extension.mkdir(parents=True)
    command = str(sys.executable).replace("\\", "\\\\")
    manifest = [
        f'name = "{name}"',
        'version = "1.0.0"',
        "protocol = 1",
        "permissions = [" + ", ".join(f'"{value}"' for value in permissions) + "]",
        "",
        "[extension]",
        f'command = ["{command}", "extension.py"]',
    ]
    for kind, capability in capabilities:
        manifest.extend(
            [
                "",
                "[[capabilities]]",
                f'kind = "{kind}"',
                f'name = "{capability}"',
            ]
        )
    (extension / "insitu-extension.toml").write_text(
        "\n".join(manifest) + "\n",
        encoding="utf-8",
    )
    (extension / "extension.py").write_text(code, encoding="utf-8")
    with (project / "insitu.toml").open("a", encoding="utf-8") as stream:
        stream.write(f'\n[[extensions]]\npath = "tools/{slug}"\n')


def _append_region(project: Path, source: str, renderer: str = "jinja") -> None:
    with (project / "README.md").open("a", encoding="utf-8") as stream:
        stream.write(
            f"""
<!-- insitu:begin materialize
id = "{source}"
source = "{source}"
renderer = "{renderer}"
template = "@insitu/json"
-->
stale
<!-- insitu:end -->
"""
        )


async def _run(project: Path) -> tuple[Reconciler, ReconcileReport]:
    reconciler = Reconciler(project, write=True)
    return reconciler, await reconciler.run()


async def test_process_transform_and_resource_callback(
    project: Path,
) -> None:
    _write_extension(
        project,
        "test/hello",
        (("transform", "hello"),),
        """from pydantic import BaseModel
from insitu.sdk import Context, Extension

class Options(BaseModel):
    name: str

app = Extension(name="test/hello", version="1.0.0")

@app.transform("hello", params=Options, result=str)
async def hello(options: Options, context: Context) -> str:
    files = await context.resource("fs:glob:**/*.md")
    return f"Hello **{options.name}** ({len(files)} files in {context.root.name})."

app.run()
""",
    )
    with (project / "README.md").open("a", encoding="utf-8") as stream:
        stream.write(
            """
<!-- insitu:begin hello
id = "hello"
name = "Ada"
-->
stale
<!-- insitu:end -->
"""
        )
    reconciler, report = await _run(project)
    try:
        assert report.ok
    finally:
        await reconciler.close()
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "Hello **Ada** (3 files in project)." in readme


async def test_typed_resource_renderer_and_validator(
    project: Path,
) -> None:
    _write_extension(
        project,
        "test/typed",
        (
            ("resource", "titles"),
            ("renderer", "bullets"),
            ("validator", "readme"),
        ),
        """from insitu.sdk import Context, Extension
from insitu.types import Diagnostic, Severity

app = Extension(name="test/typed", version="1.0.0")

@app.resource("titles", result=list[str])
async def titles(_options: dict, context: Context) -> list[str]:
    return list(await context.resource("fs:glob:**/*.md"))

@app.renderer("bullets", value=list[str])
async def bullets(value: list[str], _options: dict, _context: Context) -> str:
    return "\\n".join(f"- {item}" for item in value)

@app.validator("readme")
async def validate(_options: dict, context: Context) -> tuple[Diagnostic, ...]:
    await context.resource("fs:file:README.md")
    return (Diagnostic(severity=Severity.INFO, message="typed validator ran"),)

app.run()
""",
    )
    _append_region(project, "titles", "bullets")
    reconciler, report = await _run(project)
    try:
        assert report.ok
    finally:
        await reconciler.close()
    assert any(item.message == "typed validator ran" for item in report.diagnostics)
    assert "- README.md" in (project / "README.md").read_text(encoding="utf-8")


async def test_resource_cycle_reports_trace(
    project: Path,
) -> None:
    _write_extension(
        project,
        "test/cycle",
        (("resource", "loop"),),
        """from insitu.sdk import Context, Extension

app = Extension(name="test/cycle", version="1.0.0")

@app.resource("loop")
async def loop(_options: dict, context: Context):
    return await context.resource("resource:loop")

app.run()
""",
        permissions=("resource:read:loop",),
    )
    _append_region(project, "loop")
    reconciler, report = await _run(project)
    try:
        assert not report.ok
    finally:
        await reconciler.close()
    assert any(
        "resource:loop -> resource:loop" in item.message for item in report.diagnostics
    )


async def test_glob_subscription_invalidates_on_new_file(
    project: Path,
) -> None:
    _write_extension(
        project,
        "test/count",
        (("resource", "count"),),
        """from insitu.sdk import Context, Extension

app = Extension(name="test/count", version="1.0.0")

@app.resource("count", result=int)
async def count(_options: dict, context: Context) -> int:
    return len(await context.resource("fs:glob:**/*.md"))

app.run()
""",
    )
    _append_region(project, "count")
    reconciler = Reconciler(project, write=True)
    try:
        first = await reconciler.run()
        added = project / "notes.md"
        added.write_text("# Notes\n", encoding="utf-8")
        second = await reconciler.run({added})
    finally:
        await reconciler.close()
    assert first.ok and second.ok
    assert second.changed == ("README.md",)
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "```json\n4\n```" in readme


async def test_manifest_schema_must_match_runtime(
    project: Path,
) -> None:
    _write_extension(
        project,
        "test/schema",
        (("transform", "typed"),),
        """from insitu.sdk import Extension

app = Extension(name="test/schema", version="1.0.0")

@app.transform("typed")
def typed(_options: dict, _context) -> str:
    return "ok"

app.run()
""",
    )
    manifest = project / "tools" / "schema" / "insitu-extension.toml"
    manifest.write_text(
        manifest.read_text() + '\n[capabilities.params_schema]\ntype = "integer"\n'
    )
    reconciler = Reconciler(project, write=True)
    try:
        with pytest.raises(RuntimeError, match="runtime params schema mismatch"):
            await reconciler.run()
    finally:
        await reconciler.close()
