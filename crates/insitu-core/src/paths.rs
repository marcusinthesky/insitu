//! All host-mediated paths are confined to a canonical project root.
use anyhow::{Context, Result, ensure};
use std::{
    fs,
    path::{Component, Path, PathBuf},
};

pub(crate) fn relative(raw: &str) -> Result<&Path> {
    let path = Path::new(raw);
    ensure!(
        !path.is_absolute()
            && !path.components().any(|c| matches!(
                c,
                Component::ParentDir | Component::Prefix(_) | Component::RootDir
            )),
        "path must remain inside the project: {raw}"
    );
    Ok(path)
}

pub(crate) fn inside(root: &Path, raw: &str) -> Result<PathBuf> {
    let path = root
        .join(relative(raw)?)
        .canonicalize()
        .with_context(|| format!("resolve {raw}"))?;
    ensure!(path.starts_with(root), "path escapes project root: {raw}");
    Ok(path)
}

/// Include paths may contain `..` when the resolved path stays in the project.
pub(crate) fn from_location(root: &Path, directory: &str, raw: &str) -> Result<PathBuf> {
    ensure!(
        !Path::new(raw).is_absolute(),
        "path escapes project root: {raw}"
    );
    let base = root.join(relative(directory)?);
    let path = base.join(raw).canonicalize()?;
    ensure!(path.starts_with(root), "path escapes project root: {raw}");
    Ok(path)
}

/// Validate the nearest existing ancestor before creating a new path.
pub(crate) fn inside_for_write(root: &Path, raw: &str) -> Result<PathBuf> {
    let path = root.join(relative(raw)?);
    let ancestor = path
        .ancestors()
        .find(|p| p.exists())
        .context("no existing ancestor")?;
    ensure!(
        ancestor.canonicalize()?.starts_with(root),
        "write path escapes project root: {raw}"
    );
    if let Ok(metadata) = fs::symlink_metadata(&path) {
        ensure!(
            !metadata.file_type().is_symlink(),
            "refusing to replace symlink: {raw}"
        );
    }
    Ok(path)
}

pub(crate) fn posix(path: &Path) -> Result<String> {
    Ok(path
        .to_str()
        .context("non-UTF-8 paths are not supported by the v1 JSON protocol")?
        .replace('\\', "/"))
}

pub(crate) fn relative_name(root: &Path, path: &Path) -> Result<String> {
    posix(path.strip_prefix(root).context("path is outside project")?)
}

/// Markdown is recognized by its `.md` extension, ignoring case.
pub(crate) fn is_markdown(path: &Path) -> bool {
    path.extension()
        .is_some_and(|extension| extension.eq_ignore_ascii_case("md"))
}
