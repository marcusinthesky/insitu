//! Configuration owns defaults and validation, not execution.
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub(crate) const DEFAULT_CONFIG: &str = r#"version = 1

[sources]
include = ["**/*.md"]
respect_gitignore = true
exclude = [".git/**", ".insitu/**", ".venv/**", ".pytest_cache/**", ".ruff_cache/**", ".pyrefly/**", "**/__pycache__/**", "dist/**", "site/**"]

[paths]
models = "insitu/models"
templates = "insitu/templates"
state = ".insitu/index.db"

[watch]
debounce_ms = 25
step_ms = 5
"#;
const DEFAULT_MODEL: &str = "select\n  path,\n  coalesce(title, name) as title,\n  status\nfrom documents\nwhere status is not null and status != 'done'\norder by path;\n";
const DEFAULT_TEMPLATE: &str = "| Item | Status |\n| --- | --- |\n{% for row in rows -%}\n| [{{ row.title }}]({{ row.path | relative_to(this.directory) }}) | {{ row.status }} |\n{% else -%}\n| _None_ | |\n{% endfor %}\n";

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Sources {
    pub include: Vec<String>,
    pub respect_gitignore: bool,
    pub exclude: Vec<String>,
}
impl Default for Sources {
    fn default() -> Self {
        Self {
            include: vec!["**/*.md".into()],
            respect_gitignore: true,
            exclude: [
                ".git/**",
                ".insitu/**",
                ".venv/**",
                ".pytest_cache/**",
                ".ruff_cache/**",
                ".pyrefly/**",
                "**/__pycache__/**",
                "dist/**",
                "site/**",
            ]
            .into_iter()
            .map(str::to_owned)
            .collect(),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Paths {
    pub models: String,
    pub templates: String,
    pub state: String,
}
impl Default for Paths {
    fn default() -> Self {
        Self {
            models: "insitu/models".into(),
            templates: "insitu/templates".into(),
            state: ".insitu/index.db".into(),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Watch {
    pub debounce_ms: u64,
    pub step_ms: u64,
}
impl Default for Watch {
    fn default() -> Self {
        Self {
            debounce_ms: 25,
            step_ms: 5,
        }
    }
}
fn enabled() -> bool {
    true
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExtensionSource {
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default = "enabled")]
    pub enabled: bool,
    #[serde(default)]
    pub allow_replace: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub version: u8,
    pub sources: Sources,
    pub paths: Paths,
    pub watch: Watch,
    pub extensions: Vec<ExtensionSource>,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            version: 1,
            sources: Sources::default(),
            paths: Paths::default(),
            watch: Watch::default(),
            extensions: Vec::new(),
        }
    }
}
impl Config {
    pub fn load(root: &Path) -> Result<Self> {
        let text = fs::read_to_string(root.join("insitu.toml")).context("read insitu.toml")?;
        let config: Self = toml::from_str(&text).context("invalid insitu.toml")?;
        config.validate()?;
        Ok(config)
    }
    fn validate(&self) -> Result<()> {
        ensure!(
            self.version == 1,
            "unsupported config version {}; expected 1",
            self.version
        );
        for path in [&self.paths.models, &self.paths.templates, &self.paths.state] {
            crate::paths::relative(path)?;
        }
        ensure!(
            self.watch.debounce_ms <= 10_000,
            "debounce_ms exceeds 10000"
        );
        ensure!(
            (1..=10_000).contains(&self.watch.step_ms),
            "step_ms must be between 1 and 10000"
        );
        for source in &self.extensions {
            ensure!(
                source.path.is_some() != source.source.is_some(),
                "extension requires exactly one of path or source"
            );
        }
        Ok(())
    }
}

/// Locate the nearest configuration, accepting a file or directory as a start.
pub fn find_project(start: &Path) -> Result<PathBuf> {
    let start = start
        .canonicalize()
        .with_context(|| format!("locate {}", start.display()))?;
    let directory = if start.is_file() {
        start.parent().context("file has no parent")?
    } else {
        &start
    };
    directory
        .ancestors()
        .find(|dir| dir.join("insitu.toml").is_file())
        .map(Path::to_owned)
        .with_context(|| format!("no insitu.toml found from {}", start.display()))
}

/// Create defaults without replacing existing SQL or template definitions.
pub fn initialize_project(root: &Path, force: bool) -> Result<Vec<PathBuf>> {
    fs::create_dir_all(root)?;
    let root = root.canonicalize()?;
    let config = root.join("insitu.toml");
    if config.exists() && !force {
        bail!("{} already exists", config.display());
    }
    fs::write(&config, DEFAULT_CONFIG)?;
    let mut created = vec![config];
    for (name, text) in [
        ("insitu/models/open_items.sql", DEFAULT_MODEL),
        ("insitu/templates/table.md.j2", DEFAULT_TEMPLATE),
    ] {
        let path = crate::paths::inside_for_write(&root, name)?;
        fs::create_dir_all(path.parent().context("definition has no parent")?)?;
        if !path.exists() {
            fs::write(&path, text)?;
            created.push(path);
        }
    }
    let state = crate::paths::inside_for_write(&root, ".insitu")?;
    if !state.exists() {
        fs::create_dir_all(&state)?;
        created.push(state);
    }
    let ignore = crate::paths::inside_for_write(&root, ".gitignore")?;
    let current = if ignore.exists() {
        fs::read_to_string(&ignore)?
    } else {
        String::new()
    };
    if !current.lines().any(|line| line == ".insitu/") {
        let prefix = current.trim_end();
        fs::write(
            ignore,
            if prefix.is_empty() {
                ".insitu/\n".into()
            } else {
                format!("{prefix}\n.insitu/\n")
            },
        )?;
    }
    Ok(created)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_and_unknown_fields() {
        let c: Config = toml::from_str("version=1").unwrap();
        c.validate().unwrap();
        assert_eq!(c.paths.models, "insitu/models");
        assert!(toml::from_str::<Config>("versoin=1").is_err());
        let c: Config = toml::from_str("[paths]\nstate='../outside'").unwrap();
        assert!(c.validate().is_err());
    }
    #[test]
    fn init_does_not_replace_definitions() {
        let temp = tempfile::tempdir().unwrap();
        initialize_project(temp.path(), false).unwrap();
        let model = temp.path().join("insitu/models/open_items.sql");
        fs::write(&model, "select 42").unwrap();
        initialize_project(temp.path(), true).unwrap();
        assert_eq!(fs::read_to_string(model).unwrap(), "select 42");
        assert!(initialize_project(temp.path(), false).is_err());
    }
}
