//! A narrow, project-relative libgit2 adapter, matching the Python provider.
use crate::paths;
use anyhow::{Context, Result, bail};
use git2::{Repository, Sort};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};

pub(crate) struct Git {
    root: PathBuf,
    repository: Option<Repository>,
}
impl Git {
    pub(crate) fn open(root: &Path) -> Self {
        let repository = Repository::discover(root)
            .ok()
            .filter(|repo| repo.workdir().is_some());
        Self {
            root: root.into(),
            repository,
        }
    }
    fn repository_path(&self, relative: &str) -> Result<PathBuf> {
        let relative = paths::relative(relative)?;
        let repo = self.repository.as_ref().context("not a Git worktree")?;
        Ok(self
            .root
            .join(relative)
            .strip_prefix(
                repo.workdir()
                    .context("bare Git repository")?
                    .canonicalize()?,
            )?
            .to_owned())
    }
    pub(crate) fn is_ignored(&self, relative: &str, is_dir: bool) -> bool {
        let Some(repo) = &self.repository else {
            return false;
        };
        let Ok(path) = self
            .repository_path(relative)
            .and_then(|path| paths::posix(&path))
        else {
            return false;
        };
        repo.is_path_ignored(if is_dir { format!("{path}/") } else { path })
            .unwrap_or(false)
    }
    fn tracked(&self) -> Result<Vec<String>> {
        let Some(repo) = &self.repository else {
            return Ok(Vec::new());
        };
        let root = repo.workdir().context("bare repository")?.canonicalize()?;
        let mut paths = BTreeSet::new();
        for entry in repo.index()?.iter() {
            let name = std::str::from_utf8(&entry.path).context("non-UTF-8 Git index path")?;
            let absolute = root.join(name);
            if absolute.starts_with(&self.root) {
                paths.insert(paths::relative_name(&self.root, &absolute)?);
            }
        }
        Ok(paths.into_iter().collect())
    }
    fn head(&self) -> Result<Value> {
        let Some(repo) = &self.repository else {
            return Ok(Value::Null);
        };
        if repo.is_empty()? {
            return Ok(Value::Null);
        }
        let head = repo.head()?;
        Ok(
            json!({"oid":head.target().map(|id| id.to_string()), "ref":head.name().ok(), "branch":head.shorthand().ok(), "detached":repo.head_detached()?}),
        )
    }
    fn status(&self, relative: &str) -> Result<Value> {
        paths::relative(relative)?;
        let mut flags = Vec::new();
        if let Some(repo) = &self.repository {
            let path = self.repository_path(relative)?;
            let status = repo
                .status_file(&path)
                .unwrap_or_else(|_| git2::Status::empty())
                .bits();
            for (mask, name) in [
                (1, "index_new"),
                (2, "index_modified"),
                (4, "index_deleted"),
                (8, "index_renamed"),
                (16, "index_typechange"),
                (128, "worktree_new"),
                (256, "worktree_modified"),
                (512, "worktree_deleted"),
                (1024, "worktree_typechange"),
                (2048, "worktree_renamed"),
                (4096, "worktree_unreadable"),
                (32768, "conflicted"),
            ] {
                if status & mask != 0 {
                    flags.push(name);
                }
            }
        }
        Ok(
            json!({"path":relative,"flags":flags,"tracked":self.tracked()?.iter().any(|p| p == relative),"ignored":self.is_ignored(relative,false)}),
        )
    }
    fn blob(&self, revision: &str, relative: &str) -> Result<Value> {
        paths::relative(relative)?;
        let Some(repo) = &self.repository else {
            return Ok(Value::Null);
        };
        let path = self.repository_path(relative)?;
        let Ok(tree) = repo
            .revparse_single(revision)
            .and_then(|object| object.peel_to_tree())
        else {
            return Ok(Value::Null);
        };
        let Ok(entry) = tree.get_path(&path) else {
            return Ok(Value::Null);
        };
        let Ok(blob) = repo.find_blob(entry.id()) else {
            return Ok(Value::Null);
        };
        Ok(
            json!({"revision":revision,"path":relative,"oid":blob.id().to_string(),"content":String::from_utf8_lossy(blob.content())}),
        )
    }
    fn history(&self, relative: &str) -> Result<Value> {
        paths::relative(relative)?;
        let Some(repo) = &self.repository else {
            return Ok(json!([]));
        };
        if repo.is_empty()? {
            return Ok(json!([]));
        }
        let path = self.repository_path(relative)?;
        let mut walk = repo.revwalk()?;
        walk.set_sorting(Sort::TIME)?;
        walk.push_head()?;
        let mut previous = None;
        let mut result = Vec::new();
        for oid in walk {
            let commit = repo.find_commit(oid?)?;
            let current = commit.tree()?.get_path(&path).ok().map(|entry| entry.id());
            if current == previous {
                continue;
            }
            previous = current;
            let committed_at = chrono::DateTime::from_timestamp(commit.time().seconds(), 0)
                .context("Git commit timestamp out of range")?
                .to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
            result.push(json!({"oid":commit.id().to_string(),"summary":commit.message().unwrap_or("").lines().next().unwrap_or(""),"author":commit.author().name().unwrap_or(""),"committed_at":committed_at,"path":relative}));
            if result.len() >= 20 {
                break;
            }
        }
        Ok(Value::Array(result))
    }
    fn diff(&self, relative: &str) -> Result<Value> {
        paths::relative(relative)?;
        let Some(repo) = &self.repository else {
            return Ok(json!({"path":relative,"patch":""}));
        };
        if repo.is_empty()? {
            return Ok(json!({"path":relative,"patch":""}));
        }
        let path = self.repository_path(relative)?;
        let tree = repo.head()?.peel_to_tree()?;
        let diff = repo.diff_tree_to_workdir_with_index(Some(&tree), None)?;
        let mut output = Vec::new();
        diff.print(git2::DiffFormat::Patch, |delta, _, line| {
            if delta.old_file().path() != Some(path.as_path())
                && delta.new_file().path() != Some(path.as_path())
            {
                return true;
            }
            if matches!(line.origin(), '+' | '-' | ' ') {
                output.push(line.origin() as u8);
            }
            output.extend_from_slice(line.content());
            true
        })?;
        Ok(json!({"path":relative,"patch":String::from_utf8_lossy(&output)}))
    }
    fn ignore_rules(&self) -> Result<Value> {
        let Some(repo) = &self.repository else {
            return Ok(json!({"repository":null,"paths":[]}));
        };
        let mut files = Vec::new();
        for entry in walkdir::WalkDir::new(&self.root).follow_links(false) {
            let entry = entry?;
            if entry.file_type().is_file() && entry.file_name() == ".gitignore" {
                files.push(paths::relative_name(&self.root, entry.path())?);
            }
        }
        if repo.path().join("info/exclude").is_file() {
            files.push(".git/info/exclude".into());
        }
        files.sort();
        Ok(json!({"repository":paths::posix(repo.path())?.trim_end_matches('/'),"paths":files}))
    }
    pub(crate) fn resource(&self, resource: &str) -> Result<Value> {
        let name = resource
            .strip_prefix("git://")
            .context("not a Git resource")?
            .trim_start_matches('/');
        match name {
            "HEAD" => self.head(),
            "branch" => {
                let h = self.head()?;
                if h.is_null() {
                    Ok(h)
                } else {
                    Ok(
                        json!({"name":h["branch"],"ref":h["ref"],"oid":h["oid"],"detached":h["detached"]}),
                    )
                }
            }
            "tracked" => Ok(serde_json::to_value(self.tracked()?)?),
            "ignore-rules" => self.ignore_rules(),
            name if name.starts_with("status/") => self.status(&name[7..]),
            name if name.starts_with("diff/") => self.diff(&name[5..]),
            name if name.starts_with("history/") => self.history(&name[8..]),
            name if name.starts_with("blob/") => {
                let (rev, path) = name[5..]
                    .split_once('/')
                    .context("Git blob resource requires revision/path")?;
                self.blob(rev, path)
            }
            _ => bail!("unsupported Git resource: {resource}"),
        }
    }
}
