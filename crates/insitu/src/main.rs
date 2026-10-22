//! A thin CLI: parsing and presentation live here, not inside the engine.
use anyhow::{Context, Result};
use clap::{Args, CommandFactory, Parser, Subcommand};
use insitu_core::{
    Installer, Mode, Project, ReconcileReport, Severity, find_project, initialize_project,
};
use notify::{Event, EventKind, RecursiveMode, Watcher};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    process::ExitCode,
    time::Duration,
};
use tokio::sync::mpsc::{self, UnboundedReceiver};

/// Exit code when `check` finds stale generated content.
const STALE: u8 = 1;
/// Exit code for configuration, document and extension errors.
const ERROR: u8 = 2;

type Events = UnboundedReceiver<notify::Result<Event>>;

#[derive(Parser)]
#[command(
    about = "Keep generated repository content in situ.",
    disable_version_flag = true
)]
struct Cli {
    /// Print the version and exit.
    #[arg(long)]
    version: bool,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Args)]
struct ProjectArgs {
    /// Search for insitu.toml from this path instead of the current directory.
    #[arg(long)]
    root: Option<PathBuf>,
}

#[derive(Subcommand)]
enum Command {
    /// Create insitu.toml and starter definitions.
    Init {
        #[arg(default_value = ".")]
        directory: PathBuf,
        /// Rewrite an existing insitu.toml; SQL and template definitions are kept.
        #[arg(long)]
        force: bool,
    },
    /// Converge all materializations once.
    Sync {
        #[command(flatten)]
        project: ProjectArgs,
        /// Print the report as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Compute without writing; exit 1 when stale, 2 on an error.
    Check {
        #[command(flatten)]
        project: ProjectArgs,
        /// Print the report as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Converge now, then react to native filesystem events.
    Watch {
        #[command(flatten)]
        project: ProjectArgs,
    },
    /// Install and inspect trusted process extensions.
    Extension {
        #[command(subcommand)]
        command: ExtensionCommand,
    },
}

#[derive(Subcommand)]
enum ExtensionCommand {
    /// Install an extension and its dependencies, then lock and enable them.
    Add {
        /// A directory, `github:owner/repo[@revision]` or `git:url[#revision]`.
        source: String,
        /// A version constraint the extension must satisfy, such as `>=1.0`.
        #[arg(long)]
        version: Option<String>,
        #[command(flatten)]
        project: ProjectArgs,
    },
    /// List installed extensions after verifying their content hashes.
    List {
        #[command(flatten)]
        project: ProjectArgs,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    match run(Cli::parse()).await {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("error: {error:#}");
            ExitCode::from(ERROR)
        }
    }
}

async fn run(cli: Cli) -> Result<u8> {
    if cli.version {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return Ok(0);
    }
    let Some(command) = cli.command else {
        Cli::command().print_help()?;
        println!();
        return Ok(ERROR);
    };
    match command {
        Command::Init { directory, force } => {
            for path in initialize_project(&directory, force)? {
                println!("create {}", path.display());
            }
            Ok(0)
        }
        Command::Sync { project, json } => once(project.root, Mode::Sync, json).await,
        Command::Check { project, json } => once(project.root, Mode::Check, json).await,
        Command::Watch { project } => {
            watch(project.root).await?;
            Ok(0)
        }
        Command::Extension {
            command:
                ExtensionCommand::Add {
                    source,
                    version,
                    project,
                },
        } => {
            let installer = Installer::new(&root_path(project.root)?)?;
            let item = installer.add(&source, version.as_deref())?;
            println!("installed {} {}", item.manifest.name, item.manifest.version);
            Ok(0)
        }
        Command::Extension {
            command: ExtensionCommand::List { project },
        } => {
            let items = Installer::new(&root_path(project.root)?)?.list()?;
            if items.is_empty() {
                println!("No installed extensions.");
            }
            for item in items {
                println!(
                    "{}\t{}\t{}",
                    item.manifest.name, item.manifest.version, item.lock.source
                );
            }
            Ok(0)
        }
    }
}

fn root_path(root: Option<PathBuf>) -> Result<PathBuf> {
    let start = match root {
        Some(root) => root,
        None => std::env::current_dir()?,
    };
    find_project(&start)
}

async fn once(root: Option<PathBuf>, mode: Mode, json: bool) -> Result<u8> {
    let mut project = Project::open(root_path(root)?, mode)?;
    let result = project.run().await;
    project.close().await;
    let report = result?;
    print_report(&report, json)?;
    Ok(if report.hard_errors() {
        ERROR
    } else if mode == Mode::Check && !report.changed.is_empty() {
        STALE
    } else {
        0
    })
}

fn print_report(report: &ReconcileReport, json: bool) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string(report)?);
        return Ok(());
    }
    for diagnostic in &report.diagnostics {
        let severity = match diagnostic.severity {
            Severity::Error => "error",
            Severity::Warning => "warning",
            Severity::Info => "info",
        };
        let mut location = diagnostic.path.clone().unwrap_or_default();
        if let Some(line) = diagnostic.line {
            location = format!("{location}:{line}");
        }
        if !location.is_empty() {
            location.push_str(": ");
        }
        println!("{severity} {location}{}", diagnostic.message);
    }
    for path in &report.changed {
        println!("changed {path}");
    }
    let status = if report.ok() { "ok" } else { "failed" };
    println!(
        "{status} · {} views · {} sources",
        report.materializations, report.scanned
    );
    Ok(())
}

async fn watch(root: Option<PathBuf>) -> Result<()> {
    let root = root_path(root)?;
    let mut project = Project::open(&root, Mode::Sync)?;
    let (sender, mut events) = mpsc::unbounded_channel();
    let mut watcher = notify::recommended_watcher(move |event: notify::Result<Event>| {
        let _ = sender.send(event);
    })?;
    watcher.watch(&root, RecursiveMode::Recursive)?;
    println!("watching {}", root.display());
    let result = watch_loop(&root, &mut project, &mut events).await;
    project.close().await;
    result
}

/// Converge, then converge again after each batch of relevant changes, until Ctrl-C.
async fn watch_loop(root: &Path, project: &mut Project, events: &mut Events) -> Result<()> {
    let mut owned = BTreeMap::new();
    let report = project.run().await?;
    remember(root, &report, &mut owned)?;
    print_report(&report, false)?;
    loop {
        let changed = tokio::select! {
            _ = tokio::signal::ctrl_c() => return Ok(()),
            changed = next_batch(events, project.debounce()) => changed?,
        };
        let relevant: Vec<_> = changed
            .into_iter()
            .filter(|path| relevant_event(root, path, &owned))
            .collect();
        if relevant.is_empty() {
            continue;
        }
        if relevant.contains(&root.join("insitu.toml")) {
            project.close().await;
            *project = Project::open(root, Mode::Sync)?;
        }
        match project.run().await {
            Ok(report) => {
                remember(root, &report, &mut owned)?;
                if !report.changed.is_empty() || !report.diagnostics.is_empty() {
                    print_report(&report, false)?;
                }
            }
            Err(error) => eprintln!("error: {error:#}"),
        }
    }
}

/// Wait for a change, then collect the further changes made within `debounce`.
async fn next_batch(events: &mut Events, debounce: Duration) -> Result<Vec<PathBuf>> {
    let mut changed = Vec::new();
    while changed.is_empty() {
        changed = changed_paths(events.recv().await.context("watcher stopped")??);
    }
    let deadline = tokio::time::sleep(debounce);
    tokio::pin!(deadline);
    loop {
        tokio::select! {
            () = &mut deadline => return Ok(changed),
            event = events.recv() => match event {
                Some(event) => changed.extend(changed_paths(event?)),
                None => return Ok(changed),
            },
        }
    }
}

/// The paths an event touched; access events change nothing.
fn changed_paths(event: Event) -> Vec<PathBuf> {
    if matches!(event.kind, EventKind::Access(_)) {
        Vec::new()
    } else {
        event.paths
    }
}

fn relevant_event(root: &Path, path: &Path, owned: &BTreeMap<PathBuf, Vec<u8>>) -> bool {
    let Ok(relative) = path.strip_prefix(root) else {
        return false;
    };
    if relative.components().any(|part| {
        [".insitu", ".venv", "node_modules", "target", "__pycache__"]
            .iter()
            .any(|name| part.as_os_str() == *name)
    }) {
        return false;
    }
    if path.file_name().is_some_and(|name| {
        let name = name.to_string_lossy();
        name.starts_with(".insitu-") && name.ends_with(".tmp")
    }) {
        return false;
    }
    !owned
        .get(path)
        .is_some_and(|bytes| std::fs::read(path).is_ok_and(|current| current == *bytes))
}

fn remember(
    root: &Path,
    report: &ReconcileReport,
    owned: &mut BTreeMap<PathBuf, Vec<u8>>,
) -> Result<()> {
    for path in &report.changed {
        let path = root.join(path);
        owned.insert(path.clone(), std::fs::read(path)?);
    }
    Ok(())
}
