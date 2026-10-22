//! A concrete, single-owner document materialization engine.
//!
//! Read [`Project::run`] to follow a pass: scan, index, plan, render, prepare,
//! commit. Domain calculations are synchronous; only process I/O is async.
mod builtins;
mod config;
mod document;
mod extensions;
mod git;
mod graph;
mod index;
mod installer;
mod patch;
mod paths;
mod plan;
mod policy;
mod project;
mod values;

pub use config::{find_project, initialize_project};
pub use insitu_protocol::{ReconcileReport, Severity};
pub use installer::{InstalledExtension, Installer};
pub use patch::Mode;
pub use project::Project;
