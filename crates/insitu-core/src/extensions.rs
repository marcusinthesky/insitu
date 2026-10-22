//! Manifest validation, capability composition, and supervised subprocesses.
//! Domain work remains in Project; a process never receives direct write access
//! through this API (but is an ordinary trusted OS process, not a sandbox).
use crate::{builtins::Builtin, config::Config, graph::Graph, installer::Installer, paths};
use anyhow::{Context, Result, bail, ensure};
use insitu_protocol::{
    CapabilityKind, CapabilitySpec, CompositionMode, ExtensionManifest, Incoming, InitializeResult,
    Peer,
};
use pep440_rs::{Version, VersionSpecifiers};
use std::{
    collections::{BTreeMap, BTreeSet, btree_map::Entry},
    fs,
    path::{Path, PathBuf},
    process::Stdio,
    str::FromStr,
    time::Duration,
};
use tokio::{
    process::{Child, Command},
    sync::mpsc,
};

pub(crate) const BUILTIN_PACKAGES: &[&str] = &[
    "include",
    "jinja",
    "sql",
    "tree",
    "insitu/include",
    "insitu/jinja",
    "insitu/sql",
    "insitu/tree",
];

pub(crate) fn load_manifest(path: &Path) -> Result<ExtensionManifest> {
    let manifest: ExtensionManifest = toml::from_str(&fs::read_to_string(path)?)
        .with_context(|| format!("invalid extension manifest {}", path.display()))?;
    ensure!(
        !manifest.name.is_empty() && !manifest.version.is_empty(),
        "extension identity cannot be empty"
    );
    ensure!(
        !manifest.extension.command.is_empty() && !manifest.extension.command[0].is_empty(),
        "extension command cannot be empty"
    );
    paths::relative(&manifest.extension.cwd)?;
    let mut capabilities = BTreeSet::new();
    for spec in &manifest.capabilities {
        ensure!(!spec.name.is_empty(), "capability name cannot be empty");
        ensure!(
            capabilities.insert((spec.kind, &spec.name)),
            "duplicate capability: {}",
            spec.name
        );
        let targeted = matches!(
            spec.mode,
            CompositionMode::Decorate | CompositionMode::Replace
        );
        ensure!(
            targeted == spec.target.as_ref().is_some_and(|t| !t.is_empty()),
            "composition requires target exactly for decorate/replace"
        );
        ensure!(
            !spec.before.iter().any(|v| spec.after.contains(v)),
            "decorator cannot be both before and after an item"
        );
    }
    let mut dependencies = BTreeSet::new();
    for dep in &manifest.dependencies {
        ensure!(
            dependencies.insert((dep.capability, &dep.name)),
            "duplicate dependency: {}",
            dep.name
        );
    }
    Ok(manifest)
}

pub(crate) fn check_version(manifest: &ExtensionManifest, constraint: &str) -> Result<()> {
    if constraint != "*" && !constraint.is_empty() {
        let required = VersionSpecifiers::from_str(constraint)?;
        ensure!(
            required.contains(&Version::from_str(&manifest.version)?),
            "{} {} does not satisfy {}",
            manifest.name,
            manifest.version,
            constraint
        );
    }
    Ok(())
}

/// Whether a capability reference, `name` or `kind:name`, names this capability.
pub(crate) fn names_capability(reference: &str, kind: CapabilityKind, name: &str) -> bool {
    reference == name
        || reference
            .strip_prefix(kind.as_str())
            .and_then(|rest| rest.strip_prefix(':'))
            == Some(name)
}

/// Whether a package provides, replaces or extends the referenced capability.
pub(crate) fn provides(manifest: &ExtensionManifest, reference: &str) -> bool {
    manifest.capabilities.iter().any(|spec| {
        let name = spec.target.as_deref().unwrap_or(&spec.name);
        names_capability(reference, spec.kind, name)
    })
}

#[derive(Clone, Debug)]
pub(crate) enum Handler {
    Builtin(Builtin),
    External { process: usize, name: String },
}

#[derive(Clone)]
pub(crate) struct Step {
    pub(crate) handler: Handler,
    pub(crate) mode: CompositionMode,
}

#[derive(Default)]
pub(crate) struct Registry {
    primary: BTreeMap<(CapabilityKind, String), Handler>,
    decorators: BTreeMap<String, BTreeMap<String, (CapabilitySpec, Handler)>>,
    contributors: BTreeMap<String, Vec<Handler>>,
}

impl Registry {
    pub(crate) fn builtins() -> Self {
        let mut this = Self::default();
        for (builtin, kind, name) in Builtin::ALL {
            this.primary
                .insert((kind, name.into()), Handler::Builtin(builtin));
        }
        this
    }

    pub(crate) fn contains(&self, reference: &str) -> bool {
        self.primary
            .keys()
            .any(|(kind, name)| names_capability(reference, *kind, name))
    }

    pub(crate) fn register(
        &mut self,
        spec: &CapabilitySpec,
        handler: Handler,
        allow_replace: &[String],
    ) -> Result<()> {
        if matches!(
            spec.mode,
            CompositionMode::Contribute | CompositionMode::Decorate
        ) && spec.kind != CapabilityKind::Transform
        {
            bail!("contribute and decorate are supported only for transforms");
        }
        let target = spec.target.as_deref().unwrap_or(&spec.name).to_owned();
        let key = (spec.kind, target.clone());
        match spec.mode {
            CompositionMode::Provide => {
                ensure!(
                    !self.primary.contains_key(&key),
                    "capability already provided: {target}"
                );
                self.primary.insert(key, handler);
            }
            CompositionMode::Replace => {
                ensure!(
                    self.primary.contains_key(&key),
                    "cannot replace missing capability: {target}"
                );
                ensure!(
                    allow_replace.contains(&target),
                    "replacement requires project approval: {target}"
                );
                self.primary.insert(key, handler);
            }
            CompositionMode::Contribute => {
                ensure!(
                    self.primary.contains_key(&key),
                    "cannot contribute to missing capability: {target}"
                );
                self.contributors.entry(target).or_default().push(handler);
            }
            CompositionMode::Decorate => {
                ensure!(
                    self.primary.contains_key(&key),
                    "cannot decorate missing capability: {target}"
                );
                match self
                    .decorators
                    .entry(target)
                    .or_default()
                    .entry(spec.name.clone())
                {
                    Entry::Occupied(_) => bail!("duplicate decorator: {}", spec.name),
                    Entry::Vacant(entry) => {
                        entry.insert((spec.clone(), handler));
                    }
                }
            }
        }
        Ok(())
    }

    pub(crate) fn steps(&self, kind: CapabilityKind, name: &str) -> Result<Vec<Step>> {
        let base = self
            .primary
            .get(&(kind, name.into()))
            .with_context(|| format!("unknown {}: {name}", kind.as_str()))?;
        let mut out = vec![Step {
            handler: base.clone(),
            mode: CompositionMode::Provide,
        }];
        if kind != CapabilityKind::Transform {
            return Ok(out);
        }
        if let Some(decorators) = self.decorators.get(name) {
            let mut graph = Graph::default();
            for (name, (spec, _)) in decorators {
                graph.node(name);
                for before in &spec.before {
                    ensure!(
                        decorators.contains_key(before),
                        "missing decorator ordering reference: {before}"
                    );
                    graph.edge(before, name)?;
                }
                for after in &spec.after {
                    ensure!(
                        decorators.contains_key(after),
                        "missing decorator ordering reference: {after}"
                    );
                    graph.edge(name, after)?;
                }
            }
            for name in graph.order()? {
                out.push(Step {
                    handler: decorators[&name].1.clone(),
                    mode: CompositionMode::Decorate,
                });
            }
        }
        for handler in self.contributors.get(name).into_iter().flatten() {
            out.push(Step {
                handler: handler.clone(),
                mode: CompositionMode::Contribute,
            });
        }
        Ok(out)
    }

    pub(crate) fn validate(&self) -> Result<()> {
        for name in self.decorators.keys() {
            self.steps(CapabilityKind::Transform, name)?;
        }
        Ok(())
    }

    pub(crate) fn validators(&self) -> Vec<String> {
        self.primary
            .keys()
            .filter(|(kind, _)| *kind == CapabilityKind::Validator)
            .map(|(_, name)| name.clone())
            .collect()
    }
}

pub(crate) struct Package {
    pub(crate) path: PathBuf,
    pub(crate) manifest: ExtensionManifest,
    pub(crate) allow_replace: Vec<String>,
}

pub(crate) fn discover(root: &Path, config: &Config) -> Result<Vec<Package>> {
    let installed = Installer::new(root)?.list()?;
    let mut candidates: Vec<_> = installed
        .into_iter()
        .map(|item| (item.path, Vec::new()))
        .collect();
    for source in config.extensions.iter().filter(|s| s.enabled) {
        ensure!(
            source.source.is_none(),
            "remote extensions must be installed with `insitu extension add`"
        );
        let path = root
            .join(source.path.as_deref().context("extension needs path")?)
            .canonicalize()?;
        candidates.push((path, source.allow_replace.clone()));
    }
    let mut packages: BTreeMap<String, Package> = BTreeMap::new();
    for (path, approval) in candidates {
        let manifest = load_manifest(&path.join("insitu-extension.toml"))?;
        match packages.entry(manifest.name.clone()) {
            Entry::Occupied(mut entry) => {
                ensure!(
                    entry.get().path == path,
                    "multiple packages named {}",
                    manifest.name
                );
                entry.get_mut().allow_replace.extend(approval);
            }
            Entry::Vacant(entry) => {
                entry.insert(Package {
                    path,
                    manifest,
                    allow_replace: approval,
                });
            }
        }
    }
    order(packages)
}

/// Packages in dependency order, after checking that each dependency is present
/// at a compatible version and each capability has exactly one provider.
fn order(mut packages: BTreeMap<String, Package>) -> Result<Vec<Package>> {
    let builtins = Registry::builtins();
    let mut graph = Graph::default();
    for (name, package) in &packages {
        graph.node(name);
        for dep in &package.manifest.dependencies {
            if !dep.capability {
                if BUILTIN_PACKAGES.contains(&dep.name.as_str()) {
                    continue;
                }
                let dependency = packages.get(&dep.name).with_context(|| {
                    format!("missing extension dependency: {name} -> {}", dep.name)
                })?;
                check_version(&dependency.manifest, &dep.version)?;
                graph.edge(name, &dep.name)?;
            } else if !builtins.contains(&dep.name) {
                let providers: Vec<_> = packages
                    .iter()
                    .filter(|(_, p)| provides(&p.manifest, &dep.name))
                    .map(|(n, _)| n)
                    .collect();
                ensure!(
                    providers.len() == 1,
                    "capability dependency {} has {} providers",
                    dep.name,
                    providers.len()
                );
                graph.edge(name, providers[0])?;
            }
        }
    }
    graph
        .order()?
        .into_iter()
        .map(|name| {
            packages
                .remove(&name)
                .context("missing package after ordering")
        })
        .collect()
}

pub(crate) struct Process {
    pub(crate) manifest: ExtensionManifest,
    pub(crate) peer: Peer,
    child: Child,
}

impl Process {
    pub(crate) fn spawn(
        package: &Package,
        sender: mpsc::UnboundedSender<Incoming>,
        id: usize,
    ) -> Result<Self> {
        let cwd = paths::inside(&package.path, &package.manifest.extension.cwd)?;
        let python = python_interpreter();
        let command: Vec<_> = package
            .manifest
            .extension
            .command
            .iter()
            .map(|part| {
                if part == "{python}" {
                    python.clone()
                } else {
                    part.clone()
                }
            })
            .collect();
        let mut child = Command::new(&command[0])
            .args(&command[1..])
            .current_dir(cwd)
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .with_context(|| format!("start extension {}", package.manifest.name))?;
        let reader = child.stdout.take().context("missing extension stdout")?;
        let writer = child.stdin.take().context("missing extension stdin")?;
        let peer = Peer::connect(reader, writer, sender, id);
        Ok(Self {
            manifest: package.manifest.clone(),
            peer,
            child,
        })
    }

    pub(crate) fn verify(&self, result: &InitializeResult) -> Result<()> {
        ensure!(
            result.name == self.manifest.name && result.version == self.manifest.version,
            "manifest/runtime identity mismatch for {}",
            self.manifest.name
        );
        for spec in &self.manifest.capabilities {
            let schema = result
                .capabilities
                .get(spec.kind.as_str())
                .and_then(|v| v.get(&spec.name))
                .context("extension did not register advertised capability")?;
            ensure!(schema.is_object(), "invalid runtime capability schemas");
            for (key, declared) in [
                ("params", &spec.params_schema),
                ("value", &spec.value_schema),
                ("result", &spec.result_schema),
            ] {
                if let Some(declared) = declared {
                    ensure!(
                        schema.get(key) == Some(declared),
                        "runtime {key} schema mismatch: {}",
                        spec.name
                    );
                }
            }
        }
        Ok(())
    }

    pub(crate) async fn close(&mut self) {
        let _ = self
            .peer
            .request(
                "insitu.shutdown",
                serde_json::Value::Null,
                Duration::from_secs(2),
            )
            .await;
        self.peer.close().await;
        if tokio::time::timeout(Duration::from_secs(2), self.child.wait())
            .await
            .is_err()
        {
            let _ = self.child.kill().await;
        }
    }
}

fn python_interpreter() -> String {
    if let Ok(value) = std::env::var("INSITU_PYTHON") {
        return value;
    }
    let (scripts, python) = if cfg!(windows) {
        ("Scripts", "python.exe")
    } else {
        ("bin", "python")
    };
    // The active virtualenv, then the one a wheel installed this binary into,
    // which is where the Python engine's own interpreter would have been.
    let active = std::env::var_os("VIRTUAL_ENV").map(|root| PathBuf::from(root).join(scripts));
    let installed = std::env::current_exe().ok().and_then(|exe| {
        let dir = exe.parent()?;
        dir.parent()?
            .join("pyvenv.cfg")
            .is_file()
            .then(|| dir.to_path_buf())
    });
    for dir in active.into_iter().chain(installed) {
        let candidate = dir.join(python);
        if candidate.is_file() {
            return candidate.to_string_lossy().into_owned();
        }
    }
    if cfg!(windows) {
        "python".into()
    } else {
        "python3".into()
    }
}

pub(crate) fn resource_allowed(resource: &str, permissions: &[String]) -> bool {
    for (prefix, permission_prefix) in [
        ("fs:file:", "fs:read:"),
        ("fs:glob:", "fs:read:"),
        ("git://", "git:read:"),
        ("sql:model:", "sql:read:"),
        ("resource:", "resource:read:"),
    ] {
        let Some(value) = resource.strip_prefix(prefix) else {
            continue;
        };
        let value = value.split('?').next().unwrap_or(value);
        return permissions
            .iter()
            .filter_map(|p| p.strip_prefix(permission_prefix))
            .any(|pattern| matches_pattern(value, pattern));
    }
    false
}

fn matches_pattern(value: &str, pattern: &str) -> bool {
    let matches = |pattern: &str| {
        globset::GlobBuilder::new(pattern)
            .literal_separator(false)
            .backslash_escape(false)
            .build()
            .is_ok_and(|glob| glob.compile_matcher().is_match(value))
    };
    matches(pattern) || pattern.strip_prefix("**/").is_some_and(matches)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn permissions_are_narrow() {
        let permissions = vec!["fs:read:**/*.md".into()];
        assert!(resource_allowed("fs:file:README.md", &permissions));
        assert!(resource_allowed("fs:file:docs/a.md", &permissions));
        assert!(!resource_allowed("fs:file:secret.txt", &permissions));
        assert!(!resource_allowed("git://HEAD", &permissions));
    }

    #[test]
    fn missing_replacement_approval_fails() {
        let mut registry = Registry::builtins();
        let spec: CapabilitySpec = serde_json::from_value(serde_json::json!({"kind":"transform","name":"new","mode":"replace","target":"include"})).unwrap();
        assert!(
            registry
                .register(
                    &spec,
                    Handler::External {
                        process: 0,
                        name: "new".into()
                    },
                    &[]
                )
                .is_err()
        );
    }
}
