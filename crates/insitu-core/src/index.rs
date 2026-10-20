//! Disposable SQLite projection. User models execute under an authorizer.
use crate::{Config, Document, Mode, git::Git, paths, values};
use anyhow::{Context, Result, bail, ensure};
use ignore::gitignore::{Gitignore, GitignoreBuilder};
use rusqlite::{
    Connection,
    hooks::{AuthAction, AuthContext, Authorization},
    params,
    types::{Value as SqlValue, ValueRef},
};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::UNIX_EPOCH,
};

const SCHEMA: &str = "
pragma foreign_keys = on;
create table if not exists _documents (
 path text primary key, parent text not null, name text not null, stem text not null,
 depth integer not null, title text, body text not null, frontmatter text not null,
 content_hash text not null, mtime_ns integer not null, size integer not null);
create table if not exists entries (
 path text primary key, parent text not null, name text not null, kind text not null, depth integer not null);
create index if not exists entries_parent on entries(parent, kind, name);
";
const FIXED: &[&str] = &[
    "path",
    "parent",
    "name",
    "stem",
    "depth",
    "title",
    "body",
    "frontmatter",
    "content_hash",
];

pub(crate) struct Policy {
    root: PathBuf,
    excludes: Gitignore,
    includes: Gitignore,
    protected: Vec<String>,
    respect_gitignore: bool,
    pub(crate) git: Git,
}
/// Gitignore-style patterns with Python pathspec's directory semantics: pathspec
/// also matches `dir/**` against the directory `dir/` itself, which gitignore
/// matches only by its contents. Without this, `.insitu/**` lets `tree` list `.insitu/`.
pub(crate) fn matcher(root: &Path, patterns: &[String]) -> Result<Gitignore> {
    let mut builder = GitignoreBuilder::new(root);
    for pattern in patterns {
        builder.add_line(None, pattern)?;
        if let Some(directory) = pattern.strip_suffix("/**") {
            builder.add_line(None, &format!("{directory}/"))?;
        }
    }
    Ok(builder.build()?)
}
impl Policy {
    pub(crate) fn new(root: &Path, config: &Config) -> Result<Self> {
        Ok(Self {
            root: root.into(),
            excludes: matcher(root, &config.sources.exclude)?,
            includes: matcher(root, &config.sources.include)?,
            protected: vec![config.paths.models.clone(), config.paths.templates.clone()],
            respect_gitignore: config.sources.respect_gitignore,
            git: Git::open(root),
        })
    }
    pub(crate) fn excluded(&self, relative: &str, is_dir: bool) -> bool {
        if self
            .protected
            .iter()
            .any(|path| relative == path || relative.starts_with(&format!("{path}/")))
        {
            return false;
        }
        self.excludes
            .matched_path_or_any_parents(self.root.join(relative), is_dir)
            .is_ignore()
            || self.respect_gitignore && self.git.is_ignored(relative, is_dir)
    }
    pub(crate) fn selected(&self, relative: &str) -> bool {
        self.includes
            .matched_path_or_any_parents(self.root.join(relative), false)
            .is_ignore()
    }
}

pub(crate) struct Index {
    pub(crate) policy: Policy,
    connection: Connection,
    root: PathBuf,
}
impl Index {
    pub(crate) fn open(root: &Path, config: &Config, mode: Mode) -> Result<Self> {
        let connection = if mode == Mode::Check {
            Connection::open_in_memory()?
        } else {
            let path = paths::inside_for_write(root, &config.paths.state)?;
            fs::create_dir_all(path.parent().context("state path has no parent")?)?;
            Connection::open(path)?
        };
        connection.execute_batch(SCHEMA)?;
        Ok(Self {
            policy: Policy::new(root, config)?,
            connection,
            root: root.into(),
        })
    }

    /// Refresh the full source projection. A full scan is deliberately used until
    /// incremental selection has passed differential conformance tests.
    pub(crate) fn refresh(&mut self) -> Result<Vec<(String, Document)>> {
        let mut documents = Vec::new();
        let mut entries = Vec::new();
        let walker = walkdir::WalkDir::new(&self.root)
            .follow_links(false)
            .sort_by_file_name()
            .into_iter()
            .filter_entry(|entry| {
                if entry.depth() == 0 {
                    return true;
                }
                paths::relative_name(&self.root, entry.path())
                    .is_ok_and(|name| !self.policy.excluded(&name, entry.file_type().is_dir()))
            });
        for entry in walker {
            let entry = entry?;
            if entry.depth() == 0 || entry.file_type().is_symlink() {
                continue;
            }
            let relative = paths::relative_name(&self.root, entry.path())?;
            let kind = if entry.file_type().is_dir() {
                "directory"
            } else if entry.file_type().is_file() {
                "file"
            } else {
                continue;
            };
            entries.push((relative.clone(), kind));
            if kind == "file"
                && entry
                    .path()
                    .extension()
                    .is_some_and(|ext| ext.to_str().is_some_and(|s| s.eq_ignore_ascii_case("md")))
                && self.policy.selected(&relative)
            {
                let text =
                    fs::read_to_string(entry.path()).with_context(|| format!("read {relative}"))?;
                let document =
                    Document::parse(text).with_context(|| format!("parse {relative}"))?;
                documents.push((relative, document));
            }
        }
        documents.sort_by(|a, b| a.0.cmp(&b.0));
        let transaction = self.connection.transaction()?;
        transaction.execute("delete from _documents", [])?;
        transaction.execute("delete from entries", [])?;
        for (relative, kind) in entries {
            let (parent, name) = split_path(&relative);
            transaction.execute(
                "insert into entries(path,parent,name,kind,depth) values(?,?,?,?,?)",
                params![
                    relative,
                    parent,
                    name,
                    kind,
                    relative.split('/').count() as i64
                ],
            )?;
        }
        let mut keys = BTreeSet::new();
        for (relative, document) in &documents {
            let frontmatter = document.source_frontmatter();
            keys.extend(frontmatter.keys().cloned());
            let frontmatter = values::python_json(&Value::Object(frontmatter));
            let body = document.source_body();
            let hash = hex::encode(Sha256::digest(format!("{frontmatter}{body}")));
            let (parent, name) = split_path(relative);
            let stem = Path::new(name)
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or(name);
            let title = document
                .frontmatter
                .get("title")
                .filter(|v| !v.is_null())
                .map(values::python_str);
            let metadata = fs::metadata(self.root.join(relative))?;
            let mtime = i64::try_from(metadata.modified()?.duration_since(UNIX_EPOCH)?.as_nanos())
                .context("mtime overflows SQLite integer")?;
            transaction.execute("insert into _documents(path,parent,name,stem,depth,title,body,frontmatter,content_hash,mtime_ns,size) values(?,?,?,?,?,?,?,?,?,?,?)", params![relative,parent,name,stem,if parent.is_empty(){0}else{parent.split('/').count() as i64},title,body,frontmatter,hash,mtime,i64::try_from(metadata.len())?])?;
        }
        let mut projections: Vec<String> = FIXED.iter().map(|s| (*s).into()).collect();
        for key in keys {
            if FIXED.contains(&key.as_str()) {
                continue;
            }
            let identifier = key.replace('"', "\"\"");
            let json_path = format!("$.{}", serde_json::to_string(&key)?).replace('\'', "''");
            projections.push(format!(
                "json_extract(frontmatter, '{json_path}') as \"{identifier}\""
            ));
        }
        transaction.execute_batch(&format!(
            "drop view if exists documents; create view documents as select {} from _documents;",
            projections.join(", ")
        ))?;
        transaction.commit()?;
        Ok(documents)
    }

    pub(crate) fn query(&self, sql: &str, bindings: &Map<String, Value>) -> Result<Vec<Value>> {
        let mut prefix = sql.trim_start();
        while prefix.starts_with("--") {
            prefix = prefix
                .split_once('\n')
                .map_or("", |(_, rest)| rest)
                .trim_start();
        }
        let lower = prefix.to_ascii_lowercase();
        ensure!(
            lower.starts_with("select") || lower.starts_with("with"),
            "SQL models must be a SELECT or WITH query"
        );
        self.connection.authorizer(Some(authorize));
        let result = self.query_authorized(sql, bindings);
        self.connection
            .authorizer(None::<fn(AuthContext<'_>) -> Authorization>);
        result
    }
    fn query_authorized(&self, sql: &str, bindings: &Map<String, Value>) -> Result<Vec<Value>> {
        let mut statement = self.connection.prepare(sql)?;
        ensure!(statement.readonly(), "SQL models must be read-only");
        for index in 1..=statement.parameter_count() {
            let name = statement
                .parameter_name(index)
                .context("SQL models require named parameters")?;
            let key = name.trim_start_matches([':', '@', '$']);
            let value = bindings
                .get(key)
                .with_context(|| format!("missing SQL parameter: {name}"))?;
            let value = match value {
                Value::Null => SqlValue::Null,
                Value::Bool(value) => SqlValue::Integer(i64::from(*value)),
                Value::Number(value) => {
                    if let Some(v) = value.as_i64() {
                        SqlValue::Integer(v)
                    } else if value.is_f64() {
                        SqlValue::Real(value.as_f64().context("invalid number")?)
                    } else {
                        bail!("SQL integer out of range");
                    }
                }
                Value::String(value) => SqlValue::Text(value.clone()),
                _ => bail!("SQL parameters must be scalar values: {key}"),
            };
            statement.raw_bind_parameter(index, value)?;
        }
        let columns: Vec<String> = statement
            .column_names()
            .iter()
            .map(|s| (*s).into())
            .collect();
        let mut cursor = statement.raw_query();
        let mut rows = Vec::new();
        while let Some(row) = cursor.next()? {
            let mut object = Map::new();
            for (index, name) in columns.iter().enumerate() {
                let value = match row.get_ref(index)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(value) => Value::from(value),
                    ValueRef::Real(value) => serde_json::Number::from_f64(value)
                        .map(Value::Number)
                        .context("non-finite SQL result")?,
                    ValueRef::Text(value) => Value::String(std::str::from_utf8(value)?.into()),
                    ValueRef::Blob(_) => bail!("binary SQL results cannot cross the JSON protocol"),
                };
                object.insert(name.clone(), value);
            }
            rows.push(Value::Object(object));
        }
        Ok(rows)
    }
}
fn split_path(path: &str) -> (&str, &str) {
    path.rsplit_once('/').unwrap_or(("", path))
}
fn authorize(context: AuthContext<'_>) -> Authorization {
    match context.action {
        AuthAction::Select | AuthAction::Read { .. } | AuthAction::Recursive => {
            Authorization::Allow
        }
        AuthAction::Function { function_name }
            if !function_name.eq_ignore_ascii_case("load_extension") =>
        {
            Authorization::Allow
        }
        _ => Authorization::Deny,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn directory_globs_exclude_the_directory_like_pathspec() {
        let temp = tempfile::tempdir().unwrap();
        let policy = Policy::new(temp.path(), &Config::default()).unwrap();
        assert!(policy.excluded(".insitu", true));
        assert!(policy.excluded(".insitu/index.db", false));
        assert!(!policy.excluded("insitu", true));
    }
    #[test]
    fn projected_columns_and_authorizer() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(
            temp.path().join("a.md"),
            "---\nstatus: open\ngenerated: {hidden: 1}\n---\n# A\n",
        )
        .unwrap();
        let mut index = Index::open(temp.path(), &Config::default(), Mode::Check).unwrap();
        index.refresh().unwrap();
        let rows = index
            .query("select status from documents", &Map::new())
            .unwrap();
        assert_eq!(rows[0]["status"], "open");
        assert!(
            index
                .query("with x as (select 1) delete from _documents", &Map::new())
                .is_err()
        );
        assert!(
            index
                .query("select load_extension('anything')", &Map::new())
                .is_err()
        );
        assert_eq!(
            index
                .query("select count(*) as n from documents", &Map::new())
                .unwrap()[0]["n"],
            1
        );
        assert!(!temp.path().join(".insitu").exists());
    }
}
