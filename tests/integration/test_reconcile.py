"""End-to-end reconciliation tests."""

from pathlib import Path

import pytest

from insitu.reconcile import Reconciler

pytestmark = pytest.mark.integration


async def _run(root: Path, *, write: bool):
    reconciler = Reconciler(root, write=write)
    try:
        return await reconciler.run()
    finally:
        await reconciler.close()


async def test_sync_then_check_is_idempotent(project: Path) -> None:
    report = await _run(project, write=True)
    assert report.ok
    assert report.changed == ("README.md",)
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "[Alpha](tasks/alpha.md)" in readme
    assert "generated:\n  counts:\n    total: 2" in readme
    assert "```python\ndef answer()" in readme
    check = await _run(project, write=False)
    assert check.ok
    assert not check.changed


async def test_check_reports_stale(project: Path) -> None:
    report = await _run(project, write=False)
    assert not report.ok
    assert report.changed == ("README.md",)
    assert any(item.code == "stale" for item in report.diagnostics)


async def test_watch_follow_up_is_idempotent(project: Path) -> None:
    reconciler = Reconciler(project, write=True)
    try:
        first = await reconciler.run()
        second = await reconciler.run({project / "README.md"})
    finally:
        await reconciler.close()
    assert first.ok and second.ok
    assert not second.changed


async def test_directory_event_indexes_nested_markdown(project: Path) -> None:
    """A new directory watcher event recursively indexes its Markdown files."""
    reconciler = Reconciler(project, write=True)
    try:
        await reconciler.run()
        directory = project / "notes"
        directory.mkdir()
        note = directory / "nested.md"
        note.write_text("# Nested\n", encoding="utf-8")
        report = await reconciler.run({directory})
        indexed = reconciler.index.document_paths()
    finally:
        await reconciler.close()
    assert report.ok
    assert note in indexed


async def test_manual_generated_edit_is_reconciled(project: Path) -> None:
    reconciler = Reconciler(project, write=True)
    try:
        await reconciler.run()
        readme = project / "README.md"
        content = readme.read_text(encoding="utf-8")
        readme.write_text(
            content.replace("[Alpha]", "[Edited]"),
            encoding="utf-8",
        )
        report = await reconciler.run({readme})
    finally:
        await reconciler.close()
    assert report.ok
    assert report.changed == ("README.md",)
    assert "[Alpha]" in readme.read_text(encoding="utf-8")


async def test_self_include_is_a_cycle(project: Path) -> None:
    readme = project / "README.md"
    with readme.open("a", encoding="utf-8") as stream:
        stream.write(
            """
<!-- insitu:begin include
id = "cycle"
path = "README.md"
raw = true
-->
stale
<!-- insitu:end -->
"""
        )
    report = await _run(project, write=True)
    assert not report.ok
    assert any("dependency cycle" in item.message for item in report.diagnostics)


async def test_frontmatter_source_fields_cannot_be_targets(project: Path) -> None:
    readme = project / "README.md"
    text = readme.read_text(encoding="utf-8").replace(
        "pointer: /generated/counts",
        "pointer: /status",
    )
    readme.write_text(text, encoding="utf-8")
    report = await _run(project, write=True)
    assert not report.ok
    assert any(
        "frontmatter targets must be under /generated/" in item.message
        for item in report.diagnostics
    )
