//! Which project paths are sources: include/exclude globs, protected
//! definition directories and, optionally, `.gitignore`.
use crate::{config::Config, git::Git};
use anyhow::Result;
use ignore::gitignore::{Gitignore, GitignoreBuilder};
use std::path::{Path, PathBuf};

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
}
