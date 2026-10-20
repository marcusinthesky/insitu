//! Stage first, validate snapshots, then atomically replace each file.
use anyhow::{Context, Result, bail, ensure};
use std::{
    collections::BTreeSet,
    fs,
    io::Write,
    path::{Path, PathBuf},
};
use tempfile::NamedTempFile;

/// Both modes compute the same changes. Check never commits them.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Check,
    Sync,
}

/// A compare-before-write update. Original bytes are also a rollback image.
#[derive(Clone, Debug)]
pub(crate) struct FileChange {
    pub(crate) path: PathBuf,
    pub(crate) original: Vec<u8>,
    pub(crate) replacement: String,
}

#[derive(Default, Debug)]
pub(crate) struct ChangeSet {
    pub(crate) files: Vec<FileChange>,
}

impl ChangeSet {
    /// Validate all inputs before producing any authored-file mutation.
    fn validate(&self) -> Result<()> {
        let mut paths = BTreeSet::new();
        for change in &self.files {
            ensure!(
                paths.insert(change.path.clone()),
                "duplicate output path: {}",
                change.path.display()
            );
            ensure!(
                !fs::symlink_metadata(&change.path)?.file_type().is_symlink(),
                "refusing to replace a symlink: {}",
                change.path.display()
            );
            ensure!(
                fs::read(&change.path)? == change.original,
                "document changed since planning: {}",
                change.path.display()
            );
        }
        Ok(())
    }

    /// Stage every file on its destination filesystem. Rename failure attempts
    /// rollback. This is not a crash-atomic transaction across multiple files.
    pub(crate) fn commit(&self) -> Result<()> {
        self.validate()?;
        let mut staged = Vec::new();
        for change in &self.files {
            staged.push(stage(&change.path, change.replacement.as_bytes())?);
        }
        self.validate()?;
        for (index, (change, temporary)) in self.files.iter().zip(staged).enumerate() {
            if !fs::read(&change.path).is_ok_and(|bytes| bytes == change.original) {
                let rollback = self.rollback(index);
                bail!(
                    "document changed before commit: {}; rollback: {}",
                    change.path.display(),
                    rollback.map_or_else(|e| e.to_string(), |()| "complete".into())
                );
            }
            if let Err(error) = temporary.persist(&change.path) {
                let rollback = self.rollback(index);
                bail!(
                    "replace {}: {}; rollback: {}",
                    change.path.display(),
                    error.error,
                    rollback.map_or_else(|e| e.to_string(), |()| "complete".into())
                );
            }
        }
        Ok(())
    }

    fn rollback(&self, committed: usize) -> Result<()> {
        for change in self.files[..committed].iter().rev() {
            ensure!(
                fs::read(&change.path)? == change.replacement.as_bytes(),
                "refusing rollback over a concurrent edit: {}",
                change.path.display()
            );
            stage(&change.path, &change.original)?
                .persist(&change.path)
                .map_err(|e| e.error)?;
        }
        Ok(())
    }
}

/// Write `content` to a temporary file beside `path`, keeping the permissions
/// of any existing file. Persisting the result replaces `path` atomically.
pub(crate) fn stage(path: &Path, content: &[u8]) -> Result<NamedTempFile> {
    let parent = path.parent().context("output has no parent")?;
    let mut file = tempfile::Builder::new()
        .prefix(".insitu-")
        .suffix(".tmp")
        .tempfile_in(parent)?;
    if let Ok(metadata) = fs::metadata(path) {
        file.as_file().set_permissions(metadata.permissions())?;
    }
    file.write_all(content)?;
    file.as_file().sync_all()?;
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_input_blocks_all_writes() {
        let dir = tempfile::tempdir().unwrap();
        let a = dir.path().join("a");
        let b = dir.path().join("b");
        fs::write(&a, "old").unwrap();
        fs::write(&b, "concurrent").unwrap();
        let changes = ChangeSet {
            files: vec![
                FileChange {
                    path: a.clone(),
                    original: b"old".to_vec(),
                    replacement: "new".into(),
                },
                FileChange {
                    path: b,
                    original: b"old".to_vec(),
                    replacement: "new".into(),
                },
            ],
        };
        assert!(changes.commit().is_err());
        assert_eq!(fs::read_to_string(a).unwrap(), "old");
    }

    #[test]
    fn atomic_replace_preserves_contents() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("a");
        fs::write(&path, "before").unwrap();
        ChangeSet {
            files: vec![FileChange {
                path: path.clone(),
                original: b"before".to_vec(),
                replacement: "after🦀".into(),
            }],
        }
        .commit()
        .unwrap();
        assert_eq!(fs::read_to_string(path).unwrap(), "after🦀");
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 1);
    }
}
