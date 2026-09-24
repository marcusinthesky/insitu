"""Typer/Rich command-line interface."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from insitu import __version__
from insitu.config import find_project, initialize_project, load_config
from insitu.installer import ExtensionInstaller
from insitu.reconcile import Reconciler
from insitu.types import Diagnostic, ReconcileReport, Severity
from insitu.watcher import watch_project

app = typer.Typer(
    help="Keep generated repository content in situ.",
    invoke_without_command=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
extension_app = typer.Typer(
    help="Install and inspect extensions.",
    no_args_is_help=True,
)
app.add_typer(extension_app, name="extension")
console = Console()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", is_eager=True),
) -> None:
    """Run Insitu."""
    if version:
        console.print(__version__)
        raise typer.Exit


@app.command()
def init(
    directory: Path = typer.Argument(Path(), file_okay=False),
    force: bool = typer.Option(False, help="Replace an existing configuration."),
) -> None:
    """Create ``insitu.toml`` and definition directories."""
    try:
        created = initialize_project(directory.resolve(), force=force)
    except FileExistsError as error:
        console.print(f"[red]error[/red] {error}")
        raise typer.Exit(2) from error
    for path in created:
        console.print(f"[green]create[/green] {path.relative_to(directory.resolve())}")


@app.command("sync")
def sync_command(
    directory: Path | None = typer.Option(None, "--root", file_okay=False),
) -> None:
    """Converge all materializations once."""
    report = asyncio.run(_run_once(directory, write=True))
    _print_report(report)
    if not report.ok:
        raise typer.Exit(2)


@app.command()
def check(
    directory: Path | None = typer.Option(None, "--root", file_okay=False),
) -> None:
    """Validate without writing; fail when output is stale."""
    report = asyncio.run(_run_once(directory, write=False))
    _print_report(report)
    if _hard_errors(report):
        raise typer.Exit(2)
    if report.changed:
        raise typer.Exit(1)


@app.command()
def watch(
    directory: Path | None = typer.Option(None, "--root", file_okay=False),
) -> None:
    """Converge now, then react to native filesystem events."""
    root = find_project(directory)
    config = load_config(root)

    async def run() -> None:
        reconciler = Reconciler(root, write=True)
        console.print(f"[bold]watching[/bold] {root}")
        try:
            await watch_project(root, config, reconciler, _watch_report)
        finally:
            await reconciler.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/dim]")


@extension_app.command("add")
def extension_add(
    source: str = typer.Argument(
        help="Path, github:owner/repo@tag, or git:<url>#<revision>."
    ),
    version: str | None = typer.Option(None, help="PEP 440 version constraint."),
    directory: Path | None = typer.Option(None, "--root", file_okay=False),
) -> None:
    """Install and lock an extension and its dependencies."""
    root = find_project(directory)
    item = ExtensionInstaller(root).add(source, version=version)
    console.print(
        f"[green]installed[/green] {item.manifest.name} {item.manifest.version}"
    )


@extension_app.command("list")
def extension_list(
    directory: Path | None = typer.Option(None, "--root", file_okay=False),
) -> None:
    """List locked extensions."""
    root = find_project(directory)
    items = ExtensionInstaller(root).list()
    if not items:
        console.print("[dim]No installed extensions.[/dim]")
        return
    table = Table("Extension", "Version", "Source", box=None)
    for item in items:
        table.add_row(item.manifest.name, item.manifest.version, item.lock.source)
    console.print(table)


async def _run_once(directory: Path | None, *, write: bool) -> ReconcileReport:
    root = find_project(directory)
    reconciler = Reconciler(root, write=write)
    try:
        return await reconciler.run()
    finally:
        await reconciler.close()


def _print_report(report: ReconcileReport) -> None:
    for diagnostic in report.diagnostics:
        _print_diagnostic(diagnostic)
    for path in report.changed:
        console.print(f"[yellow]changed[/yellow] {path}")
    status = "[green]ok[/green]" if report.ok else "[red]failed[/red]"
    console.print(
        f"{status} · {report.materializations} views · {report.scanned} sources"
    )


async def _watch_report(report: ReconcileReport) -> None:
    if report.changed or report.diagnostics:
        _print_report(report)


def _print_diagnostic(diagnostic: Diagnostic) -> None:
    style = {
        Severity.ERROR: "red",
        Severity.WARNING: "yellow",
        Severity.INFO: "blue",
    }[diagnostic.severity]
    location = diagnostic.path or ""
    if diagnostic.line:
        location += f":{diagnostic.line}"
    prefix = f"{location}: " if location else ""
    console.print(
        f"[{style}]{diagnostic.severity}[/{style}] {prefix}{diagnostic.message}"
    )


def _hard_errors(report: ReconcileReport) -> bool:
    return any(
        item.severity is Severity.ERROR and item.code != "stale"
        for item in report.diagnostics
    )
