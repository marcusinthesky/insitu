//! A narrow, project-relative libgit2 adapter, matching the Python provider.
use crate::paths;
use anyhow::{Context, Result, bail};
use git2::{Repository, Sort, Status};
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
        Ok(json!({
            "oid": head.target().map(|id| id.to_string()),
            "ref": head.name().ok(),
            "branch": head.shorthand().ok(),
            "detached": repo.head_detached()?,
        }))
    }

    fn branch(&self) -> Result<Value> {
        let head = self.head()?;
        if head.is_null() {
            return Ok(head);
        }
        Ok(json!({
            "name": head["branch"],
            "ref": head["ref"],
            "oid": head["oid"],
            "detached": head["detached"],
        }))
    }

    fn status(&self, relative: &str) -> Result<Value> {
        paths::relative(relative)?;
        let mut flags = Vec::new();
        if let Some(repo) = &self.repository {
            let path = self.repository_path(relative)?;
            let status = repo.status_file(&path).unwrap_or_else(|_| Status::empty());
            for (flag, name) in [
                (Status::INDEX_NEW, "index_new"),
                (Status::INDEX_MODIFIED, "index_modified"),
                (Status::INDEX_DELETED, "index_deleted"),
                (Status::INDEX_RENAMED, "index_renamed"),
                (Status::INDEX_TYPECHANGE, "index_typechange"),
                (Status::WT_NEW, "worktree_new"),
                (Status::WT_MODIFIED, "worktree_modified"),
                (Status::WT_DELETED, "worktree_deleted"),
                (Status::WT_TYPECHANGE, "worktree_typechange"),
                (Status::WT_RENAMED, "worktree_renamed"),
                (Status::WT_UNREADABLE, "worktree_unreadable"),
                (Status::CONFLICTED, "conflicted"),
            ] {
                if status.contains(flag) {
                    flags.push(name);
                }
            }
        }
        Ok(json!({
            "path": relative,
            "flags": flags,
            "tracked": self.tracked()?.iter().any(|p| p == relative),
            "ignored": self.is_ignored(relative, false),
        }))
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
        Ok(json!({
            "revision": revision,
            "path": relative,
            "oid": blob.id().to_string(),
            "content": String::from_utf8_lossy(blob.content()),
        }))
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
            let committed_at = format_commit_timestamp(commit.time().seconds())?;
            result.push(json!({
                "oid": commit.id().to_string(),
                "summary": commit.message().unwrap_or("").lines().next().unwrap_or(""),
                "author": commit.author().name().unwrap_or(""),
                "committed_at": committed_at,
                "path": relative,
            }));
            if result.len() >= 20 {
                break;
            }
        }
        Ok(Value::Array(result))
    }

    fn diff(&self, relative: &str) -> Result<Value> {
        paths::relative(relative)?;
        let Some(repo) = &self.repository else {
            return Ok(json!({"path": relative, "patch": ""}));
        };
        if repo.is_empty()? {
            return Ok(json!({"path": relative, "patch": ""}));
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
        Ok(json!({"path": relative, "patch": String::from_utf8_lossy(&output)}))
    }

    fn ignore_rules(&self) -> Result<Value> {
        let Some(repo) = &self.repository else {
            return Ok(json!({"repository": null, "paths": []}));
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
        Ok(json!({
            "repository": paths::posix(repo.path())?.trim_end_matches('/'),
            "paths": files,
        }))
    }

    pub(crate) fn resource(&self, resource: &str) -> Result<Value> {
        let name = resource
            .strip_prefix("git://")
            .context("not a Git resource")?
            .trim_start_matches('/');
        match name.split_once('/') {
            Some(("status", path)) => self.status(path),
            Some(("diff", path)) => self.diff(path),
            Some(("history", path)) => self.history(path),
            Some(("blob", rest)) => {
                let (revision, path) = rest
                    .split_once('/')
                    .context("Git blob resource requires revision/path")?;
                self.blob(revision, path)
            }
            None if name == "HEAD" => self.head(),
            None if name == "branch" => self.branch(),
            None if name == "tracked" => Ok(serde_json::to_value(self.tracked()?)?),
            None if name == "ignore-rules" => self.ignore_rules(),
            _ => bail!("unsupported Git resource: {resource}"),
        }
    }
}

fn format_commit_timestamp(seconds: i64) -> Result<String> {
    let timestamp = time::OffsetDateTime::from_unix_timestamp(seconds)
        .ok()
        // Preserve chrono's original range while replacing the formatter.
        .filter(|value| (-262_143..=262_142).contains(&value.year()))
        .context("Git commit timestamp out of range")?;
    timestamp
        .format(time::macros::format_description!(
            "[year]-[month]-[day]T[hour]:[minute]:[second][offset_hour sign:mandatory]:[offset_minute]"
        ))
        .context("format Git commit timestamp")
}

#[cfg(test)]
mod tests {
    use super::*;
    use git2::{Signature, Time};

    #[test]
    fn history_preserves_second_precision_and_utc_offset() -> Result<()> {
        for (seconds, offset, expected) in [
            (0, 120, "1970-01-01T00:00:00+00:00"),
            (1, -300, "1970-01-01T00:00:01+00:00"),
            (951_782_400, 0, "2000-02-29T00:00:00+00:00"),
        ] {
            let directory = tempfile::tempdir()?;
            let root = directory.path().canonicalize()?;
            let repository = Repository::init(&root)?;
            std::fs::write(root.join("note.md"), "# Note\n")?;
            let mut index = repository.index()?;
            index.add_path(Path::new("note.md"))?;
            let tree = repository.find_tree(index.write_tree()?)?;
            let signature = Signature::new(
                "Contributor",
                "test@example.com",
                &Time::new(seconds, offset),
            )?;
            repository.commit(Some("HEAD"), &signature, &signature, "Add note", &tree, &[])?;

            let history = Git::open(&root).resource("git://history/note.md")?;
            assert_eq!(history[0]["committed_at"], expected);
        }
        Ok(())
    }

    #[test]
    fn commit_timestamp_preserves_extended_dates_and_range_errors() -> Result<()> {
        for (seconds, expected) in [
            (-1, "1969-12-31T23:59:59+00:00"),
            (253_402_300_800, "+10000-01-01T00:00:00+00:00"),
            (-8_334_601_228_800, "-262143-01-01T00:00:00+00:00"),
            (8_210_266_876_799, "+262142-12-31T23:59:59+00:00"),
        ] {
            assert_eq!(format_commit_timestamp(seconds)?, expected);
        }
        for seconds in [i64::MIN, i64::MAX, -8_334_601_228_801, 8_210_266_876_800] {
            assert_eq!(
                format_commit_timestamp(seconds).unwrap_err().to_string(),
                "Git commit timestamp out of range"
            );
        }
        Ok(())
    }
}
