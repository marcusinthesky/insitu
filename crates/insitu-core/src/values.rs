//! Small Python-compatible value conventions used at compatibility boundaries.
use anyhow::{Context, Result};
use insitu_protocol::Options;
use serde_json::Value;
use std::fmt::Write;

pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64() != Some(0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}

pub(crate) fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(v) => v.clone(),
        Value::Number(v) => v.to_string(),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(python_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(items) => format!(
            "{{{}}}",
            items
                .iter()
                .map(|(k, v)| format!(
                    "{}: {}",
                    python_repr(&Value::String(k.clone())),
                    python_repr(v)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn python_repr(value: &Value) -> String {
    if let Value::String(s) = value {
        let quote = if s.contains('\'') && !s.contains('"') {
            '"'
        } else {
            '\''
        };
        let mut out = String::new();
        out.push(quote);
        for c in s.chars() {
            match c {
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if c == quote => {
                    out.push('\\');
                    out.push(c);
                }
                c => out.push(c),
            }
        }
        out.push(quote);
        out
    } else {
        python_str(value)
    }
}

/// `json.dumps(..., sort_keys=True)`-style separators and ASCII escaping.
pub(crate) fn python_json(value: &Value) -> String {
    fn quoted(value: &str) -> String {
        let text = serde_json::to_string(value).unwrap_or_else(|_| "\"\"".into());
        let mut out = String::new();
        for ch in text.chars() {
            if ch.is_ascii() {
                out.push(ch);
            } else {
                let mut buf = [0; 2];
                for unit in ch.encode_utf16(&mut buf).iter() {
                    // Writing to a String cannot fail.
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
        out
    }
    match value {
        Value::String(v) => quoted(v),
        Value::Array(v) => format!(
            "[{}]",
            v.iter().map(python_json).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(v) => {
            let mut keys: Vec<_> = v.keys().collect();
            keys.sort();
            format!(
                "{{{}}}",
                keys.into_iter()
                    .map(|k| format!("{}: {}", quoted(k), python_json(&v[k])))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        _ => value.to_string(),
    }
}

pub(crate) fn option_string(options: &Options, name: &str, default: &str) -> String {
    options
        .get(name)
        .filter(|v| truthy(v))
        .map(python_str)
        .unwrap_or_else(|| default.into())
}

pub(crate) fn option_integer(options: &Options, name: &str, default: i64) -> Result<i64> {
    match options.get(name) {
        None => Ok(default),
        Some(Value::String(v)) => Ok(v.parse()?),
        Some(value) => value
            .as_i64()
            .with_context(|| format!("{name} must be an integer")),
    }
}

pub(crate) fn value_rows(value: Value) -> Vec<Value> {
    match value {
        Value::Array(items) => items.into_iter().map(row).collect(),
        other => vec![row(other)],
    }
}

fn row(value: Value) -> Value {
    if value.is_object() {
        value
    } else {
        serde_json::json!({"value":value})
    }
}
