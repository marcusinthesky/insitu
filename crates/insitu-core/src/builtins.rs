//! The four built-ins share ordinary data; no plugin trait hierarchy is needed.
use crate::{
    config::Paths,
    graph::Graph,
    index::Index,
    paths,
    policy::{Policy, matcher},
    values::{option_integer, option_string, python_json, python_str, truthy, value_rows},
};
use anyhow::{Context, Result, ensure};
use insitu_protocol::{CapabilityKind, InvocationResult, Location, Options};
use minijinja::{Environment, UndefinedBehavior};
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, PoisonError},
};

/// Capabilities implemented natively rather than by an extension process.
#[derive(Clone, Copy, Debug)]
pub(crate) enum Builtin {
    SqlResource,
    Jinja,
    Include,
    Tree,
    SqlTransform,
    Materialize,
}

impl Builtin {
    /// Every built-in, with the kind and name it is registered under.
    pub(crate) const ALL: [(Self, CapabilityKind, &'static str); 6] = [
        (Self::SqlResource, CapabilityKind::Resource, "sql"),
        (Self::Jinja, CapabilityKind::Renderer, "jinja"),
        (Self::Include, CapabilityKind::Transform, "include"),
        (Self::Tree, CapabilityKind::Transform, "tree"),
        (Self::SqlTransform, CapabilityKind::Transform, "sql"),
        (Self::Materialize, CapabilityKind::Transform, "materialize"),
    ];
}

pub(crate) struct Builtins {
    root: PathBuf,
    paths: Paths,
}

impl Builtins {
    pub(crate) fn new(root: &Path, paths: Paths) -> Self {
        Self {
            root: root.into(),
            paths,
        }
    }

    pub(crate) fn include(
        &self,
        options: &Options,
        location: &Location,
    ) -> Result<InvocationResult> {
        let raw = option_string(options, "path", "");
        ensure!(!raw.is_empty(), "include transform requires path");
        let path = paths::from_location(&self.root, &location.directory, &raw)?;
        let text = fs::read_to_string(&path)?;
        let lines: Vec<_> = text.lines().collect();
        let start = option_integer(options, "start", 1)?;
        let end = option_integer(options, "end", i64::try_from(lines.len())?)?;
        ensure!(
            start >= 1 && end >= start,
            "include lines require 1 <= start <= end"
        );
        let selected = lines
            .iter()
            .skip(usize::try_from(start - 1)?)
            .take(usize::try_from(end - start + 1)?)
            .copied()
            .collect::<Vec<_>>()
            .join("\n");
        let raw = options.get("raw").is_some_and(truthy);
        let language = option_string(
            options,
            "language",
            path.extension().and_then(|v| v.to_str()).unwrap_or(""),
        );
        let content = if raw {
            selected
        } else {
            format!("```{language}\n{selected}\n```")
        };
        Ok(InvocationResult {
            content: Some(content),
            dependencies: vec![format!(
                "fs:file:{}",
                paths::relative_name(&self.root, &path)?
            )],
            ..Default::default()
        })
    }

    pub(crate) fn tree(
        &self,
        options: &Options,
        location: &Location,
        policy: &Policy,
    ) -> Result<InvocationResult> {
        let depth = option_integer(options, "depth", 1)?;
        ensure!(
            (0..=32).contains(&depth),
            "tree depth must be between 0 and 32"
        );
        let base = paths::inside(&self.root, &location.directory)?;
        let directories_only = options.get("directories_only").is_some_and(truthy);
        let mut patterns = Vec::new();
        if let Some(raw) = options.get("exclude") {
            for item in raw.as_array().context("tree exclude must be an array")? {
                patterns.push(python_str(item));
            }
        }
        let extra = matcher(&self.root, &patterns)?;
        let mut lines = vec![format!(
            "{}/",
            base.file_name().and_then(|v| v.to_str()).unwrap_or("")
        )];
        Tree {
            root: &self.root,
            policy,
            extra: &extra,
            directories_only,
            depth: usize::try_from(depth)?,
        }
        .walk(&base, "", 1, &mut lines)?;
        Ok(InvocationResult {
            content: Some(format!("```text\n{}\n```", lines.join("\n"))),
            dependencies: vec![format!(
                "fs:dir:{}",
                if location.directory.is_empty() {
                    "."
                } else {
                    &location.directory
                }
            )],
            ..Default::default()
        })
    }

    pub(crate) fn query(
        &self,
        options: &Options,
        location: &Location,
        index: &Index,
    ) -> Result<InvocationResult> {
        let name = option_string(options, "model", "");
        ensure!(!name.is_empty(), "SQL materialization requires model");
        let mut compiler = Compiler {
            root: paths::inside_for_write(&self.root, &self.paths.models)?,
            rendered: BTreeMap::new(),
            graph: Graph::default(),
            files: Vec::new(),
        };
        compiler.visit(&name)?;
        let order = compiler.graph.order()?;
        let sql = compiler
            .rendered
            .get(&name)
            .context("model disappeared")?
            .clone();
        let dependencies: Vec<_> = order.iter().filter(|n| *n != &name).collect();
        let sql = if dependencies.is_empty() {
            sql
        } else {
            let mut ctes: Vec<_> = dependencies
                .into_iter()
                .map(|n| format!("{} as (\n{}\n)", alias(n), indent(&compiler.rendered[n])))
                .collect();
            ctes.push(format!("__insitu_result as (\n{}\n)", indent(&sql)));
            format!("with {}\nselect * from __insitu_result", ctes.join(",\n"))
        };
        let mut bindings = Options::new();
        bindings.insert("insitu_path".into(), location.path.clone().into());
        bindings.insert("insitu_dir".into(), location.directory.clone().into());
        bindings.insert("insitu_root".into(), location.root.clone().into());
        bindings.insert("insitu_depth".into(), location.depth.into());
        bindings.extend(params(options)?);
        let rows = index.query(&sql, &bindings)?;
        let mut dependencies = vec!["index:documents".into()];
        for path in compiler.files {
            dependencies.push(format!(
                "fs:file:{}",
                paths::relative_name(&self.root, &path)?
            ));
        }
        Ok(InvocationResult {
            value: Value::Array(rows),
            dependencies,
            ..Default::default()
        })
    }

    pub(crate) fn render(
        &self,
        value: Value,
        options: &Options,
        location: &Location,
    ) -> Result<InvocationResult> {
        let template = option_string(options, "template", "@insitu/table");
        let rows = value_rows(value.clone());
        let params = params(options)?;
        let content = match template.as_str() {
            "@insitu/table" => table(&rows),
            "@insitu/list" => {
                if rows.is_empty() {
                    "_No rows._".into()
                } else {
                    rows.iter()
                        .map(|row| format!("- {}", python_str(row)))
                        .collect::<Vec<_>>()
                        .join("\n")
                }
            }
            "@insitu/json" => format!(
                "```json\n{}\n```",
                serde_json::to_string_pretty(&value)?
                    .replace('&', "\\u0026")
                    .replace('<', "\\u003c")
                    .replace('>', "\\u003e")
                    .replace('\'', "\\u0027")
            ),
            _ => {
                let root = paths::inside_for_write(&self.root, &self.paths.templates)?;
                let mut env = Environment::new();
                env.set_undefined_behavior(UndefinedBehavior::Strict);
                env.set_keep_trailing_newline(true);
                env.set_trim_blocks(true);
                env.set_lstrip_blocks(true);
                env.set_unknown_method_callback(python_method);
                env.set_loader(move |name| {
                    let file = paths::inside(&root, name).map_err(template_error)?;
                    // LF in, so a template renders the same however it was checked out.
                    fs::read_to_string(file)
                        .map(|text| Some(text.replace("\r\n", "\n")))
                        .map_err(template_error)
                });
                env.add_filter("relative_to", |value: String, directory: Option<String>| {
                    relative_to(&value, directory.as_deref().unwrap_or(""))
                });
                env.add_filter(
                    "json",
                    |value: minijinja::Value| -> Result<String, minijinja::Error> {
                        let value = serde_json::to_value(value).map_err(template_error)?;
                        Ok(python_json(&value))
                    },
                );
                env.get_template(&template)?
                    .render(json!({"rows":rows,"params":params,"this":location,"value":value}))?
            }
        };
        let dependencies = if template.starts_with("@insitu/") {
            vec![]
        } else {
            vec![format!("fs:file:{}/{}", self.paths.templates, template)]
        };
        Ok(InvocationResult {
            content: Some(content.trim_end().into()),
            dependencies,
            ..Default::default()
        })
    }
}

fn template_error(error: impl std::fmt::Display) -> minijinja::Error {
    minijinja::Error::new(minijinja::ErrorKind::InvalidOperation, error.to_string())
}

/// Python `str` methods that Jinja2 templates call but MiniJinja's pycompat
/// layer lacks. Everything else falls through to pycompat.
fn python_method(
    state: &minijinja::State<'_, '_>,
    value: &minijinja::Value,
    method: &str,
    args: &[minijinja::Value],
) -> Result<minijinja::Value, minijinja::Error> {
    use minijinja::{Value as V, value::from_args};
    if let Some(text) = value.as_str() {
        match method {
            "rsplit" => {
                let (separator, limit): (Option<&str>, Option<i64>) = from_args(args)?;
                if separator == Some("") {
                    return Err(template_error("empty separator"));
                }
                // Python treats a negative `maxsplit` as no limit.
                let limit = limit.and_then(|limit| usize::try_from(limit).ok());
                return Ok(V::from(python_rsplit(text, separator, limit)));
            }
            "ljust" | "rjust" => {
                let (width, fill): (usize, Option<&str>) = from_args(args)?;
                let fill = fill.unwrap_or(" ");
                let mut fill_chars = fill.chars();
                if let (Some(fill), None) = (fill_chars.next(), fill_chars.next()) {
                    let padding = fill
                        .to_string()
                        .repeat(width.saturating_sub(text.chars().count()));
                    return Ok(V::from(if method == "ljust" {
                        format!("{text}{padding}")
                    } else {
                        format!("{padding}{text}")
                    }));
                }
                return Err(template_error(
                    "the fill character must be exactly one character long",
                ));
            }
            _ => {}
        }
    }
    minijinja_contrib::pycompat::unknown_method_callback(state, value, method, args)
}

/// Python's `str.rsplit(separator, maxsplit)`; a `None` limit splits everywhere.
fn python_rsplit(text: &str, separator: Option<&str>, limit: Option<usize>) -> Vec<String> {
    let Some(separator) = separator else {
        // Whitespace mode: runs of whitespace separate, and empty fields are dropped.
        let mut parts = Vec::new();
        let mut rest = text.trim_end();
        while !rest.is_empty() {
            if limit == Some(parts.len()) {
                parts.push(rest.trim_start().to_owned());
                break;
            }
            match rest.rfind(char::is_whitespace) {
                Some(index) => {
                    let width = rest[index..].chars().next().map_or(1, char::len_utf8);
                    parts.push(rest[index + width..].to_owned());
                    rest = rest[..index].trim_end();
                }
                None => {
                    parts.push(rest.to_owned());
                    break;
                }
            }
        }
        parts.reverse();
        return parts;
    };
    let mut parts: Vec<String> = match limit {
        None => text.rsplit(separator).map(str::to_owned).collect(),
        Some(limit) => text
            .rsplitn(limit + 1, separator)
            .map(str::to_owned)
            .collect(),
    };
    parts.reverse();
    parts
}

/// The `params` table, or an empty one.
fn params(options: &Options) -> Result<Options> {
    match options.get("params") {
        None => Ok(Options::new()),
        Some(value) => value.as_object().cloned().context("params must be a table"),
    }
}

struct Compiler {
    root: PathBuf,
    rendered: BTreeMap<String, String>,
    graph: Graph,
    files: Vec<PathBuf>,
}

impl Compiler {
    fn visit(&mut self, name: &str) -> Result<()> {
        if self.rendered.contains_key(name) {
            return Ok(());
        }
        let path = paths::inside(&self.root, &format!("{name}.sql"))?;
        let source =
            fs::read_to_string(&path).with_context(|| format!("unknown SQL model: {name}"))?;
        let references = Arc::new(Mutex::new(Vec::new()));
        let captured = Arc::clone(&references);
        let mut env = Environment::new();
        env.set_undefined_behavior(UndefinedBehavior::Strict);
        env.add_function("ref", move |name: String| {
            captured
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push(name.clone());
            alias(&name)
        });
        let sql = env
            .render_str(&source, ())?
            .trim()
            .trim_end_matches(';')
            .to_owned();
        self.graph.node(name);
        let dependencies = references
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        for dependency in dependencies {
            self.graph.edge(name, &dependency)?;
            self.visit(&dependency)?;
        }
        self.files.push(path);
        self.rendered.insert(name.into(), sql);
        Ok(())
    }
}

fn alias(name: &str) -> String {
    let stem: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect();
    format!(
        "__insitu_{stem}_{}",
        &hex::encode(Sha1::digest(name.as_bytes()))[..8]
    )
}

fn indent(text: &str) -> String {
    text.lines()
        .map(|line| format!("  {line}"))
        .collect::<Vec<_>>()
        .join("\n")
}

fn table(rows: &[Value]) -> String {
    let Some(first) = rows.first().and_then(Value::as_object) else {
        return "_No rows._".into();
    };
    let mut lines = vec![
        format!(
            "| {}",
            first
                .keys()
                .map(|key| format!("{key} |"))
                .collect::<String>()
        ),
        format!("|{}", " --- |".repeat(first.len())),
    ];
    for row in rows {
        let cells = row
            .as_object()
            .map(|row| {
                row.values()
                    .map(|v| format!("{} |", python_str(v)))
                    .collect::<String>()
            })
            .unwrap_or_default();
        lines.push(format!("| {cells}"));
    }
    lines.join("\n")
}

fn relative_to(value: &str, directory: &str) -> String {
    fn normalize(value: &str) -> Vec<&str> {
        let mut out = Vec::new();
        for part in value.split('/') {
            match part {
                "" | "." => {}
                ".." if out.last().is_some_and(|last| *last != "..") => {
                    out.pop();
                }
                other => out.push(other),
            }
        }
        out
    }
    let target = normalize(value);
    let base = normalize(directory);
    let shared = target.iter().zip(&base).take_while(|(a, b)| a == b).count();
    let mut parts = vec![".."; base.len() - shared];
    parts.extend_from_slice(&target[shared..]);
    if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    }
}

struct Tree<'a> {
    root: &'a Path,
    policy: &'a Policy,
    extra: &'a ignore::gitignore::Gitignore,
    directories_only: bool,
    depth: usize,
}

impl Tree<'_> {
    fn walk(
        &self,
        directory: &Path,
        prefix: &str,
        level: usize,
        lines: &mut Vec<String>,
    ) -> Result<()> {
        if level > self.depth {
            return Ok(());
        }
        let mut entries = Vec::new();
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_symlink() {
                continue;
            }
            let relative = paths::relative_name(self.root, &entry.path())?;
            if self.policy.excluded(&relative, kind.is_dir())
                || self
                    .extra
                    .matched_path_or_any_parents(entry.path(), kind.is_dir())
                    .is_ignore()
                || self.directories_only && !kind.is_dir()
            {
                continue;
            }
            entries.push((
                !kind.is_dir(),
                entry.file_name().to_string_lossy().into_owned(),
                entry.path(),
            ));
        }
        entries.sort_by_key(|(file, name, _)| (*file, name.to_lowercase()));
        for (index, (file, name, path)) in entries.iter().enumerate() {
            let last = index + 1 == entries.len();
            let marker = if last { "└── " } else { "├── " };
            lines.push(format!(
                "{prefix}{marker}{name}{}",
                if *file { "" } else { "/" }
            ));
            if !file {
                self.walk(
                    path,
                    &format!("{prefix}{}", if last { "    " } else { "│   " }),
                    level + 1,
                    lines,
                )?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relative_paths() {
        assert_eq!(relative_to("tasks/alpha.md", "docs"), "../tasks/alpha.md");
    }

    #[test]
    fn aliases_do_not_collide() {
        assert_ne!(alias("a/b"), alias("a_b"));
    }

    // Expectations are Python Jinja2 output; regenerate them with Jinja2, never by hand.
    const ORACLE: &str = include_str!("../../../tests/fixtures/templates/children.expected.json");

    #[test]
    fn python_string_methods_match_python() {
        let oracle: Value = serde_json::from_str(ORACLE).unwrap();
        let cases: [(&str, Option<&str>, Option<usize>); 6] = [
            ("a b  c ", None, None),
            ("a b  c", None, Some(1)),
            ("a,b,,c", Some(","), None),
            ("a,b,,c", Some(","), Some(1)),
            ("  ", None, None),
            ("x", None, Some(0)),
        ];
        for ((text, separator, limit), expected) in
            cases.into_iter().zip(oracle["rsplit"].as_array().unwrap())
        {
            assert_eq!(
                json!(python_rsplit(text, separator, limit)),
                *expected,
                "{text:?}.rsplit({separator:?}, {limit:?})"
            );
        }
        let mut env = Environment::new();
        env.set_unknown_method_callback(python_method);
        let rendered = env.render_str(r#"{{ ["ab".ljust(5), "ab".ljust(1), "é".ljust(3, "-"), "ab".rjust(4, "*")] | tojson }}"#, ()).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&rendered).unwrap(),
            oracle["ljust"]
        );
        assert!(env.render_str(r#"{{ "a".rsplit("") }}"#, ()).is_err());
    }

    #[test]
    fn pricing_perspective_children_template_matches_jinja2() {
        let oracle: Value = serde_json::from_str(ORACLE).unwrap();
        let dir = tempfile::tempdir().unwrap();
        // Builtins expects a canonical root (macOS /private/var, Windows \\?\).
        let root = dir.path().canonicalize().unwrap();
        let templates = root.join("insitu/templates");
        fs::create_dir_all(&templates).unwrap();
        fs::write(
            templates.join("children.md.j2"),
            include_str!("../../../tests/fixtures/templates/children.md.j2"),
        )
        .unwrap();
        let options = json!({"template":"children.md.j2"})
            .as_object()
            .cloned()
            .unwrap();
        let result = Builtins::new(&root, Paths::default())
            .render(
                oracle["rows"].clone(),
                &options,
                &Location::from_path("README.md"),
            )
            .unwrap();
        assert_eq!(result.content.as_deref(), oracle["expected"].as_str());
    }

    #[test]
    fn table_preserves_query_order() {
        assert_eq!(
            table(&[json!({"title":"Alpha","count":2})]),
            "| title |count |\n| --- | --- |\n| Alpha |2 |"
        );
    }
}
