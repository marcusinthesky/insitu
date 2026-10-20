//! A thin CLI: parsing and presentation live here, not inside the engine.
use anyhow::{Context, Result};
use clap::{CommandFactory, Parser, Subcommand};
use insitu_core::{Installer, Mode, Project, ReconcileReport, find_project, initialize_project};
use notify::Watcher;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    process::ExitCode,
    time::Duration,
};

#[derive(Parser)]
#[command(
    about = "Keep generated repository content in situ.",
    disable_version_flag = true
)]
struct Cli {
    #[arg(long)]
    version: bool,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Subcommand)]
enum Command {
    /// Create insitu.toml and starter definitions.
    Init {
        #[arg(default_value = ".")]
        directory: PathBuf,
        #[arg(long)]
        force: bool,
    },
    /// Converge all materializations once.
    Sync {
        #[arg(long)]
        root: Option<PathBuf>,
        #[arg(long)]
        json: bool,
    },
    /// Compute without writing; exit 1 when stale, 2 on an error.
    Check {
        #[arg(long)]
        root: Option<PathBuf>,
        #[arg(long)]
        json: bool,
    },
    /// Converge now, then react to native filesystem events.
    Watch {
        #[arg(long)]
        root: Option<PathBuf>,
    },
    /// Install and inspect trusted process extensions.
    Extension {
        #[command(subcommand)]
        command: ExtensionCommand,
    },
}
#[derive(Subcommand)]
enum ExtensionCommand {
    Add {
        source: String,
        #[arg(long)]
        version: Option<String>,
        #[arg(long)]
        root: Option<PathBuf>,
    },
    List {
        #[arg(long)]
        root: Option<PathBuf>,
    },
}
#[tokio::main]
async fn main() -> ExitCode {
    match run(Cli::parse()).await {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("error: {error:#}");
            ExitCode::from(2)
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
        return Ok(2);
    };
    match command {
        Command::Init { directory, force } => {
            for path in initialize_project(&directory, force)? {
                println!("create {}", path.display());
            }
            Ok(0)
        }
        Command::Sync { root, json } => once(root, Mode::Sync, json).await,
        Command::Check { root, json } => once(root, Mode::Check, json).await,
        Command::Watch { root } => {
            watch(root).await?;
            Ok(0)
        }
        Command::Extension { command } => match command {
            ExtensionCommand::Add {
                source,
                version,
                root,
            } => {
                let root = root_path(root)?;
                let item = Installer::new(&root)?.add(&source, version.as_deref())?;
                println!("installed {} {}", item.manifest.name, item.manifest.version);
                Ok(0)
            }
            ExtensionCommand::List { root } => {
                let items = Installer::new(&root_path(root)?)?.list()?;
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
        },
    }
}
fn root_path(root: Option<PathBuf>) -> Result<PathBuf> {
    find_project(&root.unwrap_or(std::env::current_dir()?))
}
async fn once(root: Option<PathBuf>, mode: Mode, json: bool) -> Result<u8> {
    let root = root_path(root)?;
    let mut project = Project::open(&root, mode)?;
    let result = project.run().await;
    project.close().await;
    let report = result?;
    print_report(&report, json)?;
    Ok(if report.hard_errors() {
        2
    } else if mode == Mode::Check && !report.changed.is_empty() {
        1
    } else {
        0
    })
}
fn print_report(report: &ReconcileReport, json_output: bool) -> Result<()> {
    if json_output {
        println!("{}", serde_json::to_string(report)?);
        return Ok(());
    }
    for diagnostic in &report.diagnostics {
        let severity = match diagnostic.severity {
            insitu_core::Severity::Error => "error",
            insitu_core::Severity::Warning => "warning",
            insitu_core::Severity::Info => "info",
        };
        let mut location = diagnostic.path.clone().unwrap_or_default();
        if let Some(line) = diagnostic.line {
            location.push_str(&format!(":{line}"));
        }
        println!(
            "{severity} {}{}",
            if location.is_empty() {
                String::new()
            } else {
                format!("{location}: ")
            },
            diagnostic.message
        );
    }
    for path in &report.changed {
        println!("changed {path}");
    }
    println!(
        "{} · {} views · {} sources",
        if report.ok() { "ok" } else { "failed" },
        report.materializations,
        report.scanned
    );
    Ok(())
}
async fn watch(root: Option<PathBuf>) -> Result<()> {
    let root = root_path(root)?;
    let mut project = Project::open(&root, Mode::Sync)?;
    let (sender, mut events) = tokio::sync::mpsc::unbounded_channel();
    let mut watcher = notify::recommended_watcher(move |event: notify::Result<notify::Event>| {
        let _ = sender.send(event);
    })?;
    watcher.watch(&root, notify::RecursiveMode::Recursive)?;
    println!("watching {}", root.display());
    let result=async {
        let mut owned=BTreeMap::new();
        let first=project.run().await?; remember(&root,&first,&mut owned)?; print_report(&first,false)?;
        loop {
            let first=tokio::select! { _=tokio::signal::ctrl_c()=>break, event=events.recv()=>event.context("watcher stopped")?? };
            if matches!(first.kind,notify::EventKind::Access(_)) { continue; }
            let mut changed=first.paths;
            let delay=tokio::time::sleep(Duration::from_millis(project.config().watch.debounce_ms)); tokio::pin!(delay);
            loop {
                tokio::select! { _=&mut delay=>break,event=events.recv()=>{
                    if let Some(event)=event { let event=event?; if !matches!(event.kind,notify::EventKind::Access(_)) { changed.extend(event.paths); } }
                } }
            }
            let relevant:Vec<_>=changed.into_iter().filter(|path| relevant_event(&root,path,&owned)).collect();
            if relevant.is_empty() { continue; }
            if relevant.iter().any(|path| path==&root.join("insitu.toml")) { project.close().await; project=Project::open(&root,Mode::Sync)?; }
            match project.run().await {
                Ok(report)=>{ remember(&root,&report,&mut owned)?; if !report.changed.is_empty() || !report.diagnostics.is_empty() { print_report(&report,false)?; } },
                Err(error)=>eprintln!("error: {error:#}"),
            }
        }
        Ok::<_,anyhow::Error>(())
    }.await;
    project.close().await;
    result
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
