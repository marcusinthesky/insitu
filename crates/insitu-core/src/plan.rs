//! Planning: documents become jobs, and rendered jobs become a change set.
use crate::{
    document::{self, Document},
    patch::{ChangeSet, FileChange},
    paths,
    values::option_string,
};
use anyhow::{Context, Result, bail, ensure};
use insitu_protocol::{InvocationResult, Options};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

/// Where a materialization writes its output.
#[derive(Clone)]
pub(crate) enum Target {
    /// Between a region's begin and end markers.
    Region,
    /// At a JSON pointer under `/generated/` in the front matter.
    Frontmatter(String),
}

#[derive(Clone)]
pub(crate) struct Job {
    pub(crate) path: String,
    pub(crate) id: String,
    pub(crate) kind: String,
    pub(crate) options: Options,
    pub(crate) line: usize,
    pub(crate) target: Target,
}

impl Job {
    pub(crate) fn node(&self) -> String {
        format!("materialization:{}#{}", self.path, self.id)
    }

    pub(crate) fn output(&self) -> String {
        match &self.target {
            Target::Frontmatter(pointer) => format!("yaml:{}#{pointer}", self.path),
            Target::Region => format!("markdown:{}#region:{}", self.path, self.id),
        }
    }
}

fn parse_target(options: &Options) -> Result<Target> {
    let pointer = match options.get("target") {
        Some(Value::String(s)) if s == "frontmatter" => {
            bail!("frontmatter target requires pointer")
        }
        Some(Value::String(s)) => s.strip_prefix("frontmatter:"),
        Some(Value::Object(map))
            if map.get("kind").and_then(Value::as_str) == Some("frontmatter") =>
        {
            Some(
                map.get("pointer")
                    .and_then(Value::as_str)
                    .context("frontmatter target requires pointer")?,
            )
        }
        _ => None,
    };
    let Some(pointer) = pointer else {
        return Ok(Target::Region);
    };
    ensure!(
        pointer.starts_with("/generated/"),
        "frontmatter targets must be under /generated/"
    );
    Ok(Target::Frontmatter(pointer.to_owned()))
}

/// One job per region and front-matter materialization, rejecting duplicate
/// identities and overlapping front-matter pointers within a document.
pub(crate) fn plan(documents: &[(String, Document)]) -> Result<Vec<Job>> {
    let mut jobs = Vec::new();
    for (path, document) in documents {
        let mut identities = BTreeSet::new();
        let mut pointers = Vec::new();
        let regions = document.regions.iter().map(|region| {
            (
                region.id.clone(),
                region.kind.clone(),
                region.options.clone(),
                region.line,
            )
        });
        let frontmatter = document
            .materializations()
            .into_iter()
            .map(|(id, options)| {
                let kind = option_string(&options, "kind", "sql");
                (id, kind, options, 1)
            });
        for (id, kind, options, line) in regions.chain(frontmatter) {
            ensure!(
                identities.insert(id.clone()),
                "multiple materializations own {path}#{id}"
            );
            let target = parse_target(&options)?;
            if let Target::Frontmatter(pointer) = &target {
                pointers.push(pointer.clone());
            }
            jobs.push(Job {
                path: path.clone(),
                id,
                kind,
                options,
                line,
                target,
            });
        }
        document::check_pointer_conflicts(&pointers)?;
    }
    Ok(jobs)
}

/// Splice rendered output into each document, keeping only the files that change.
pub(crate) fn prepare(
    root: &Path,
    documents: &[(String, Document)],
    rendered: Vec<(Job, InvocationResult)>,
) -> Result<ChangeSet> {
    let mut grouped: BTreeMap<String, Vec<(Job, InvocationResult)>> = BTreeMap::new();
    for (job, result) in rendered {
        grouped
            .entry(job.path.clone())
            .or_default()
            .push((job, result));
    }
    let mut changes = ChangeSet::default();
    for (path, document) in documents {
        let Some(results) = grouped.remove(path) else {
            continue;
        };
        let mut regions = BTreeMap::new();
        let mut frontmatter = Vec::new();
        for (job, result) in results {
            match job.target {
                Target::Frontmatter(pointer) => frontmatter.push((pointer, result.value)),
                Target::Region => {
                    regions.insert(job.id, result.content.unwrap_or_default());
                }
            }
        }
        let mut text = document::replace_regions(&document.text, &regions)?;
        if !frontmatter.is_empty() {
            text = document::replace_frontmatter(&text, &frontmatter)?;
        }
        if text != document.text {
            changes.files.push(FileChange {
                path: paths::inside(root, path)?,
                original: document.text.as_bytes().to_owned(),
                replacement: text,
            });
        }
    }
    Ok(changes)
}
