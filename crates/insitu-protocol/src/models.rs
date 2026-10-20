//! Language-neutral types. Unknown boundary fields are rejected, as in Python.

use schemars::JsonSchema;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;

/// Named, JSON-valued transform arguments.
pub type Options = Map<String, Value>;

fn protocol_one() -> u8 {
    1
}
fn dot() -> String {
    ".".into()
}
fn star() -> String {
    "*".into()
}
fn deserialize_protocol<'de, D: Deserializer<'de>>(de: D) -> Result<u8, D::Error> {
    let version = u8::deserialize(de)?;
    if version == 1 {
        Ok(version)
    } else {
        Err(serde::de::Error::custom(
            "unsupported Insitu protocol; expected 1",
        ))
    }
}

/// Repository-relative context for a materialization.
#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Location {
    pub path: String,
    pub directory: String,
    #[serde(default = "dot")]
    pub root: String,
    #[serde(default)]
    pub depth: usize,
}
impl Location {
    pub fn from_path(path: &str) -> Self {
        let directory = path.rsplit_once('/').map_or("", |(dir, _)| dir).to_owned();
        let depth = if directory.is_empty() {
            0
        } else {
            directory.split('/').count()
        };
        Self {
            path: path.into(),
            directory,
            root: dot(),
            depth,
        }
    }
}

/// Severity used in CLI reports and extension validation.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Error,
    Warning,
    Info,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Diagnostic {
    pub severity: Severity,
    pub message: String,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub line: Option<usize>,
    #[serde(default)]
    pub code: Option<String>,
}
impl Diagnostic {
    pub fn error(message: impl Into<String>) -> Self {
        Self {
            severity: Severity::Error,
            message: message.into(),
            path: None,
            line: None,
            code: None,
        }
    }
}

#[derive(
    Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, JsonSchema,
)]
#[serde(rename_all = "lowercase")]
pub enum CapabilityKind {
    Resource,
    Transform,
    Renderer,
    Validator,
}
impl CapabilityKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Resource => "resource",
            Self::Transform => "transform",
            Self::Renderer => "renderer",
            Self::Validator => "validator",
        }
    }
    pub fn method(self) -> &'static str {
        match self {
            Self::Resource => "insitu.resource",
            Self::Transform => "insitu.transform",
            Self::Renderer => "insitu.render",
            Self::Validator => "insitu.validate",
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum CompositionMode {
    #[default]
    Provide,
    Contribute,
    Decorate,
    Replace,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CapabilitySpec {
    pub kind: CapabilityKind,
    pub name: String,
    #[serde(default)]
    pub mode: CompositionMode,
    #[serde(default)]
    pub target: Option<String>,
    #[serde(default)]
    pub before: Vec<String>,
    #[serde(default)]
    pub after: Vec<String>,
    #[serde(default)]
    pub params_schema: Option<Value>,
    #[serde(default)]
    pub value_schema: Option<Value>,
    #[serde(default)]
    pub result_schema: Option<Value>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExtensionDependency {
    pub name: String,
    #[serde(default = "star")]
    pub version: String,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub capability: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExtensionCommand {
    pub command: Vec<String>,
    #[serde(default = "dot")]
    pub cwd: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExtensionManifest {
    pub name: String,
    pub version: String,
    #[serde(default = "protocol_one", deserialize_with = "deserialize_protocol")]
    pub protocol: u8,
    pub extension: ExtensionCommand,
    #[serde(default)]
    pub capabilities: Vec<CapabilitySpec>,
    #[serde(default)]
    pub dependencies: Vec<ExtensionDependency>,
    #[serde(default)]
    pub permissions: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LockedExtension {
    pub name: String,
    pub version: String,
    pub source: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<String>,
    pub sha256: String,
    #[serde(default = "protocol_one", deserialize_with = "deserialize_protocol")]
    pub protocol: u8,
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default)]
    pub permissions: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InitializeRequest {
    #[serde(default = "protocol_one", deserialize_with = "deserialize_protocol")]
    pub protocol: u8,
    pub root: String,
    pub extension: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InitializeResult {
    pub name: String,
    pub version: String,
    #[serde(default = "protocol_one", deserialize_with = "deserialize_protocol")]
    pub protocol: u8,
    #[serde(default)]
    pub capabilities: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InvocationRequest {
    pub name: String,
    #[serde(default)]
    pub options: Options,
    pub location: Location,
    #[serde(default)]
    pub value: Value,
    #[serde(default)]
    pub trace: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InvocationResult {
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub value: Value,
    #[serde(default)]
    pub diagnostics: Vec<Diagnostic>,
    #[serde(default)]
    pub dependencies: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ResourceRequest {
    pub resource: String,
    pub location: Location,
    #[serde(default)]
    pub trace: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ResourceRead {
    pub value: Value,
    #[serde(default)]
    pub dependencies: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ReconcileReport {
    pub scanned: usize,
    pub materializations: usize,
    pub changed: Vec<String>,
    pub diagnostics: Vec<Diagnostic>,
    pub elapsed_ms: f64,
}
impl ReconcileReport {
    pub fn ok(&self) -> bool {
        !self
            .diagnostics
            .iter()
            .any(|d| d.severity == Severity::Error)
    }
    pub fn hard_errors(&self) -> bool {
        self.diagnostics
            .iter()
            .any(|d| d.severity == Severity::Error && d.code.as_deref() != Some("stale"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn location_matches_python() {
        let l = Location::from_path("docs/tasks/a.md");
        assert_eq!(l.directory, "docs/tasks");
        assert_eq!(l.depth, 2);
        assert_eq!(Location::from_path("a.md").depth, 0);
    }
    #[test]
    fn boundary_rejects_unknown_fields() {
        assert!(
            serde_json::from_value::<Location>(
                serde_json::json!({"path":"x", "directory":"", "typo":1})
            )
            .is_err()
        );
    }
    #[test]
    fn handshake_rejects_future_protocol() {
        assert!(
            serde_json::from_value::<InitializeRequest>(
                serde_json::json!({"protocol":2,"root":"/x","extension":"test"})
            )
            .is_err()
        );
    }
}
