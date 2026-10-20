//! Explicit installation; merely opening a project never downloads packages.
use crate::{
    extensions::{BUILTIN_PACKAGES, Registry, check_version, load_manifest},
    graph::Graph,
    paths,
};
use anyhow::{Context, Result, bail, ensure};
use insitu_protocol::{ExtensionManifest, LockedExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
};
const IGNORED: &[&str] = &[".git", ".insitu", "__pycache__", "node_modules", "target"];

/// A verified installed extension and its original v1 lock record.
#[derive(Clone, Debug)]
pub struct InstalledExtension {
    pub manifest: ExtensionManifest,
    pub path: PathBuf,
    pub lock: LockedExtension,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct LockFile {
    version: u8,
    #[serde(default)]
    extensions: Vec<LockedExtension>,
}

/// Local/Git package resolution, content verification, and lockfile management.
pub struct Installer {
    root: PathBuf,
    destination: PathBuf,
}
impl Installer {
    pub fn new(root: &Path) -> Result<Self> {
        let root = root.canonicalize()?;
        let destination = paths::inside_for_write(&root, ".insitu/extensions")?;
        Ok(Self { root, destination })
    }
    /// Validate content hashes before returning packages in dependency order.
    pub fn list(&self) -> Result<Vec<InstalledExtension>> {
        let lock_path = self.root.join("insitu.lock");
        let lock = if lock_path.is_file() {
            toml::from_str::<LockFile>(&fs::read_to_string(lock_path)?)?
        } else {
            LockFile {
                version: 1,
                extensions: vec![],
            }
        };
        ensure!(lock.version == 1, "unsupported extension lock version");
        let mut records = BTreeMap::new();
        for record in lock.extensions {
            ensure!(
                !records.contains_key(&record.name),
                "duplicate locked extension: {}",
                record.name
            );
            records.insert(record.name.clone(), record);
        }
        let mut installed = BTreeMap::new();
        if self.destination.exists() {
            for entry in fs::read_dir(&self.destination)? {
                let entry = entry?;
                if !entry.file_type()?.is_dir()
                    || !entry.path().join("insitu-extension.toml").is_file()
                {
                    continue;
                }
                let manifest = load_manifest(&entry.path().join("insitu-extension.toml"))?;
                let record = records.remove(&manifest.name).with_context(|| {
                    format!("installed extension is not locked: {}", manifest.name)
                })?;
                ensure!(
                    record.version == manifest.version && record.protocol == manifest.protocol,
                    "extension lock metadata changed: {}",
                    manifest.name
                );
                ensure!(
                    record.sha256 == tree_hash(&entry.path())?,
                    "extension content hash changed: {}",
                    manifest.name
                );
                installed.insert(
                    manifest.name.clone(),
                    InstalledExtension {
                        manifest,
                        path: entry.path(),
                        lock: record,
                    },
                );
            }
        }
        ensure!(
            records.is_empty(),
            "locked extension is missing: {}",
            records.keys().cloned().collect::<Vec<_>>().join(", ")
        );
        let mut graph = Graph::default();
        for (name, item) in &installed {
            graph.node(name);
            for dep in &item.manifest.dependencies {
                if !dep.capability && installed.contains_key(&dep.name) {
                    graph.edge(name, &dep.name)?;
                }
            }
        }
        graph
            .order()?
            .into_iter()
            .map(|name| installed.remove(&name).context("missing ordered extension"))
            .collect()
    }

    /// Install a package and its source-addressable dependencies. Publication and
    /// extension execution are never part of installation.
    pub fn add(&self, source: &str, version: Option<&str>) -> Result<InstalledExtension> {
        let existing: BTreeMap<_, _> = self
            .list()?
            .into_iter()
            .map(|item| (item.manifest.name.clone(), item))
            .collect();
        fs::create_dir_all(&self.destination)?;
        let staging = tempfile::Builder::new()
            .prefix(".install-")
            .tempdir_in(&self.destination)?;
        let mut resolver = Resolver {
            staging: staging.path().into(),
            destination: self.destination.clone(),
            packages: existing,
            new_names: BTreeSet::new(),
            visiting: BTreeSet::new(),
            graph: Graph::default(),
        };
        let name = resolver.resolve(source, version.unwrap_or("*"), None)?;
        resolver.validate_capabilities()?;
        let packages = &resolver.packages;
        let records = packages.values().map(|item| item.lock.clone()).collect();
        let lock = toml::to_string_pretty(&LockFile {
            version: 1,
            extensions: records,
        })?;
        let config_path = self.root.join("insitu.toml");
        let original_config = fs::read(&config_path)?;
        let mut config = String::from_utf8(original_config.clone())?;
        let parsed: toml::Value = toml::from_str(&config)?;
        let configured: BTreeSet<_> = parsed
            .get("extensions")
            .and_then(toml::Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|v| v.get("path").and_then(toml::Value::as_str))
            .map(str::to_owned)
            .collect();
        for item in packages.values() {
            let relative = paths::relative_name(&self.root, &item.path)?;
            if !configured.contains(&relative) {
                config.push_str(&format!(
                    "\n[[extensions]]\npath = {}\n",
                    toml::Value::String(relative)
                ));
            }
        }
        let lock_path = self.root.join("insitu.lock");
        let original_lock = read_optional(&lock_path)?;
        ensure!(
            fs::read(&config_path)? == original_config
                && read_optional(&lock_path)? == original_lock,
            "configuration changed during installation"
        );
        let mut moved = Vec::new();
        let result = (|| -> Result<()> {
            for name in &resolver.new_names {
                let target = &packages[name].path;
                ensure!(
                    !target.exists(),
                    "extension destination already exists: {}",
                    target.display()
                );
                fs::rename(staging.path().join(slug(name)?), target)?;
                moved.push(target.clone());
            }
            write_atomic(&lock_path, lock.as_bytes())?;
            write_atomic(&config_path, config.as_bytes())?;
            Ok(())
        })();
        if let Err(error) = result {
            let mut rollback_errors = Vec::new();
            for path in moved {
                if let Err(error) = fs::remove_dir_all(path) {
                    rollback_errors.push(error.to_string());
                }
            }
            for (path, before, after) in [
                (&config_path, Some(original_config), config.as_bytes()),
                (&lock_path, original_lock, lock.as_bytes()),
            ] {
                // Do not overwrite an unrelated concurrent edit while rolling back.
                if read_optional(path)?.as_deref() == Some(after) {
                    let reverted = match before {
                        Some(bytes) => write_atomic(path, &bytes),
                        None => fs::remove_file(path).map_err(Into::into),
                    };
                    if let Err(error) = reverted {
                        rollback_errors.push(error.to_string());
                    }
                }
            }
            bail!(
                "installation failed: {error:#}; rollback: {}",
                if rollback_errors.is_empty() {
                    "completed".into()
                } else {
                    rollback_errors.join("; ")
                }
            );
        }
        packages
            .get(&name)
            .cloned()
            .context("installed package disappeared")
    }
}
struct Resolver {
    staging: PathBuf,
    destination: PathBuf,
    packages: BTreeMap<String, InstalledExtension>,
    new_names: BTreeSet<String>,
    visiting: BTreeSet<String>,
    graph: Graph,
}
impl Resolver {
    fn resolve(&mut self, source: &str, constraint: &str, parent: Option<&str>) -> Result<String> {
        let checkout = Checkout::open(source)?;
        let manifest = load_manifest(&checkout.path.join("insitu-extension.toml"))?;
        let name = manifest.name.clone();
        self.graph.node(&name);
        if let Some(parent) = parent {
            self.graph.edge(parent, &name)?;
        }
        check_version(&manifest, constraint)?;
        if let Some(existing) = self.packages.get(&name) {
            check_version(&existing.manifest, constraint)?;
            return Ok(name);
        }
        ensure!(
            self.visiting.insert(name.clone()),
            "extension dependency cycle includes {name}"
        );
        for dependency in &manifest.dependencies {
            if !dependency.capability && BUILTIN_PACKAGES.contains(&dependency.name.as_str()) {
                continue;
            }
            if let Some(existing) = self.packages.get(&dependency.name) {
                check_version(&existing.manifest, &dependency.version)?;
                self.graph.edge(&name, &dependency.name)?;
                continue;
            }
            if let Some(raw) = &dependency.source {
                let local = checkout.path.join(raw);
                let source = if local.exists() {
                    paths::posix(&local)?
                } else {
                    raw.clone()
                };
                let resolved = self.resolve(&source, &dependency.version, Some(&name))?;
                if !dependency.capability {
                    ensure!(
                        resolved == dependency.name,
                        "dependency source identity mismatch: expected {}, got {resolved}",
                        dependency.name
                    );
                }
            } else if !dependency.capability {
                bail!("dependency {} needs a source", dependency.name);
            }
        }
        let slug = slug(&name)?;
        ensure!(
            !self
                .packages
                .values()
                .any(|p| p.path.file_name().is_some_and(|n| n == slug.as_str())),
            "extension slug collision: {name}"
        );
        let stage = self.staging.join(&slug);
        copy_package(&checkout.path, &stage)?;
        let lock = LockedExtension {
            name: name.clone(),
            version: manifest.version.clone(),
            source: source.into(),
            revision: checkout.revision,
            sha256: tree_hash(&stage)?,
            protocol: 1,
            dependencies: manifest
                .dependencies
                .iter()
                .filter(|d| !d.capability)
                .map(|d| d.name.clone())
                .collect(),
            permissions: manifest.permissions.clone(),
        };
        self.packages.insert(
            name.clone(),
            InstalledExtension {
                manifest,
                path: self.destination.join(slug),
                lock,
            },
        );
        self.visiting.remove(&name);
        self.new_names.insert(name.clone());
        Ok(name)
    }
    fn validate_capabilities(&self) -> Result<()> {
        let builtins = Registry::builtins();
        for item in self.packages.values() {
            for dependency in item.manifest.dependencies.iter().filter(|d| d.capability) {
                let found = builtins.contains(&dependency.name)
                    || self.packages.values().any(|p| {
                        p.manifest.capabilities.iter().any(|s| {
                            let name = s.target.as_ref().unwrap_or(&s.name);
                            dependency.name == *name
                                || dependency.name == format!("{}:{name}", s.kind.as_str())
                        })
                    });
                ensure!(
                    found,
                    "missing capability dependency: {} -> {}",
                    item.manifest.name,
                    dependency.name
                );
            }
        }
        Ok(())
    }
}
struct Checkout {
    path: PathBuf,
    revision: Option<String>,
    _temporary: Option<tempfile::TempDir>,
}
impl Checkout {
    fn open(source: &str) -> Result<Self> {
        let source_path = if let Some(relative) = source.strip_prefix("~/") {
            PathBuf::from(std::env::var_os("HOME").context("HOME is not set")?).join(relative)
        } else {
            PathBuf::from(source)
        };
        if source_path.exists() {
            let path = source_path.canonicalize()?;
            ensure!(path.is_dir(), "extension source is not a directory");
            let revision = git2::Repository::discover(&path).ok().and_then(|r| {
                r.head()
                    .ok()
                    .and_then(|h| h.target())
                    .map(|oid| oid.to_string())
            });
            return Ok(Self {
                path,
                revision,
                _temporary: None,
            });
        }
        let (url, revision) = if let Some(raw) = source.strip_prefix("github:") {
            let (repository, revision) = raw
                .split_once('@')
                .map_or((raw, None), |(a, b)| (a, Some(b)));
            (format!("https://github.com/{repository}.git"), revision)
        } else {
            let raw = source.strip_prefix("git:").unwrap_or(source);
            raw.rsplit_once('#')
                .map_or((raw.into(), None), |(a, b)| (a.into(), Some(b)))
        };
        ensure!(
            !url.starts_with('-') && !url.is_empty(),
            "invalid Git source"
        );
        let temp = tempfile::tempdir()?;
        let path = temp.path().join("checkout");
        ensure!(
            Command::new("git")
                .args(["clone", "--quiet", "--"])
                .arg(&url)
                .arg(&path)
                .status()
                .context("remote installation requires Git on PATH")?
                .success(),
            "Git clone failed"
        );
        if let Some(revision) = revision {
            ensure!(
                !revision.starts_with('-') && !revision.is_empty(),
                "invalid Git revision"
            );
            ensure!(
                Command::new("git")
                    .arg("-C")
                    .arg(&path)
                    .args(["checkout", "--quiet", "--detach", revision, "--"])
                    .status()?
                    .success(),
                "Git checkout failed"
            );
        }
        let repo = git2::Repository::open(&path)?;
        let revision = repo.head()?.target().map(|oid| oid.to_string());
        Ok(Self {
            path,
            revision,
            _temporary: Some(temp),
        })
    }
}
fn package_files(root: &Path) -> Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    for entry in walkdir::WalkDir::new(root)
        .follow_links(false)
        .into_iter()
        .filter_entry(|e| {
            e.depth() == 0 || !IGNORED.contains(&e.file_name().to_string_lossy().as_ref())
        })
    {
        let entry = entry?;
        ensure!(
            !entry.file_type().is_symlink(),
            "extension packages may not contain symlinks: {}",
            entry.path().display()
        );
        if entry.file_type().is_file() {
            files.push(entry.path().to_owned());
        }
    }
    files.sort();
    Ok(files)
}
fn copy_package(source: &Path, target: &Path) -> Result<()> {
    fs::create_dir_all(target)?;
    for file in package_files(source)? {
        let destination = target.join(file.strip_prefix(source)?);
        fs::create_dir_all(destination.parent().context("package file has no parent")?)?;
        fs::copy(file, destination)?;
    }
    Ok(())
}
fn tree_hash(root: &Path) -> Result<String> {
    let mut hash = Sha256::new();
    for file in package_files(root)? {
        hash.update(paths::relative_name(root, &file)?.as_bytes());
        hash.update(fs::read(file)?);
    }
    Ok(hex::encode(hash.finalize()))
}
fn slug(name: &str) -> Result<String> {
    ensure!(
        !name.is_empty()
            && name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "-._/".contains(c)),
        "unsupported extension name: {name}"
    );
    let slug = name.replace(['/', '.'], "-");
    ensure!(
        slug.chars().any(|c| c.is_ascii_alphanumeric()),
        "invalid extension name: {name}"
    );
    Ok(slug)
}
fn read_optional(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}
fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    ensure!(
        !fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()),
        "refusing symlink destination"
    );
    let mut temp =
        tempfile::NamedTempFile::new_in(path.parent().context("destination has no parent")?)?;
    if let Ok(meta) = fs::metadata(path) {
        temp.as_file().set_permissions(meta.permissions())?;
    }
    temp.write_all(bytes)?;
    temp.as_file().sync_all()?;
    temp.persist(path)?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hashing_matches_original_concatenation() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("a"), b"b").unwrap();
        assert_eq!(
            tree_hash(root.path()).unwrap(),
            hex::encode(Sha256::digest(b"ab"))
        );
    }
    #[test]
    fn install_and_detect_tampering() {
        let root = tempfile::tempdir().unwrap();
        crate::initialize_project(root.path(), false).unwrap();
        let source = tempfile::tempdir().unwrap();
        fs::write(
            source.path().join("insitu-extension.toml"),
            "name='test'\nversion='1.0.0'\n[extension]\ncommand=['echo']\n",
        )
        .unwrap();
        let installer = Installer::new(root.path()).unwrap();
        let item = installer
            .add(source.path().to_str().unwrap(), None)
            .unwrap();
        assert_eq!(installer.list().unwrap().len(), 1);
        fs::write(item.path.join("changed"), "tamper").unwrap();
        assert!(installer.list().is_err());
    }
}
