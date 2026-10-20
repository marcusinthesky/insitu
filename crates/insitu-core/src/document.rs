//! Source ranges, not full-document Markdown serialization.
use anyhow::{Context, Result, bail, ensure};
use insitu_protocol::Options;
use regex::Regex;
use serde_json::{Map, Value};
use std::{collections::BTreeMap, ops::Range, str::FromStr};

#[derive(Clone, Debug)]
pub struct Region {
    pub id: String,
    pub kind: String,
    pub options: Options,
    pub line: usize,
    pub content: Range<usize>,
}

#[derive(Clone, Debug)]
pub struct Document {
    pub text: String,
    pub frontmatter: Options,
    pub body_offset: usize,
    pub regions: Vec<Region>,
    yaml_range: Option<Range<usize>>,
}
impl Document {
    pub fn parse(text: String) -> Result<Self> {
        let yaml_range = frontmatter_range(&text);
        let (frontmatter, body_offset) = if let Some((range, end)) = &yaml_range {
            (parse_yaml(&text[range.clone()])?, *end)
        } else {
            (Map::new(), 0)
        };
        let regions = parse_regions(&text, body_offset)?;
        Ok(Self {
            text,
            frontmatter,
            body_offset,
            regions,
            yaml_range: yaml_range.map(|(range, _)| range),
        })
    }
    pub fn source_body(&self) -> String {
        let mut body = self.text[self.body_offset..].to_owned();
        for region in self.regions.iter().rev() {
            body.replace_range(
                region.content.start - self.body_offset..region.content.end - self.body_offset,
                "\n",
            );
        }
        body
    }
    pub fn source_frontmatter(&self) -> Options {
        self.frontmatter
            .iter()
            .filter(|(key, _)| !["insitu", "generated"].contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect()
    }
    pub(crate) fn materializations(&self) -> Vec<(String, Options)> {
        let Some(raw) = self
            .frontmatter
            .get("insitu")
            .and_then(|v| v.get("materializations"))
            .and_then(Value::as_object)
        else {
            return Vec::new();
        };
        raw.iter()
            .filter_map(|(name, item)| {
                item.as_object().map(|item| {
                    let mut options = item.clone();
                    let id = options
                        .remove("id")
                        .and_then(|v| v.as_str().map(str::to_owned))
                        .unwrap_or_else(|| name.clone());
                    (id, options)
                })
            })
            .collect()
    }
}

fn frontmatter_range(text: &str) -> Option<(Range<usize>, usize)> {
    let start = if text.starts_with("---\r\n") {
        5
    } else if text.starts_with("---\n") {
        4
    } else {
        return None;
    };
    let mut offset = start;
    for line in text[start..].split_inclusive('\n') {
        if line.starts_with("---") && line.trim_end() == "---" {
            return Some((start..offset, offset + line.len()));
        }
        offset += line.len();
    }
    None
}

fn parse_yaml(raw: &str) -> Result<Options> {
    let mut value: serde_norway::Value =
        serde_norway::from_str(raw).context("invalid YAML front matter")?;
    value.apply_merge().context("invalid YAML merge")?;
    if value.is_null() {
        return Ok(Map::new());
    }
    let json = yaml_json(value)?;
    json.as_object()
        .cloned()
        .context("YAML front matter must be a mapping")
}
fn yaml_json(value: serde_norway::Value) -> Result<Value> {
    use serde_norway::Value as Y;
    Ok(match value {
        Y::Null => Value::Null,
        Y::Bool(value) => Value::Bool(value),
        Y::Number(value) => serde_json::to_value(value)?,
        Y::String(value) => Value::String(value),
        Y::Sequence(values) => {
            Value::Array(values.into_iter().map(yaml_json).collect::<Result<_>>()?)
        }
        Y::Mapping(values) => {
            let mut map = Map::new();
            for (key, value) in values {
                let key = yaml_json(key)?;
                let key = key
                    .as_str()
                    .map_or_else(|| crate::values::python_str(&key), str::to_owned);
                map.insert(key, yaml_json(value)?);
            }
            Value::Object(map)
        }
        Y::Tagged(value) => yaml_json(value.value)?,
    })
}

/// Parse paired markers outside fenced code. Offsets are UTF-8 byte offsets.
pub fn parse_regions(text: &str, start: usize) -> Result<Vec<Region>> {
    ensure!(
        text.is_char_boundary(start),
        "invalid Markdown start offset"
    );
    let begin =
        Regex::new(r"(?s)<!--\s*insitu:begin(?:\s+(?P<kind>[\w.-]+))?\s*\n(?P<header>.*?)-->")?;
    let end = Regex::new(r"<!--\s*insitu:end\s*-->")?;
    let fences = fenced_ranges(text, start);
    let outside = |position| !fences.iter().any(|range| range.contains(&position));
    let mut cursor = start;
    let mut regions = Vec::new();
    while let Some(capture) = begin.captures_at(text, cursor) {
        let opening = capture.get(0).context("missing directive capture")?;
        if !outside(opening.start()) {
            cursor = opening.end();
            continue;
        }
        let closing = end
            .find_iter(&text[opening.end()..])
            .map(|m| opening.end() + m.start()..opening.end() + m.end())
            .find(|m| outside(m.start));
        let line = text[..opening.start()]
            .bytes()
            .filter(|b| *b == b'\n')
            .count()
            + 1;
        let closing =
            closing.with_context(|| format!("unterminated Insitu region at line {line}"))?;
        let header: toml::Table = toml::from_str(
            capture
                .name("header")
                .context("missing header")?
                .as_str()
                .trim(),
        )?;
        let options = serde_json::to_value(header)?
            .as_object()
            .cloned()
            .context("directive must be a table")?;
        let id = options
            .get("id")
            .filter(|v| crate::values::truthy(v))
            .map(crate::values::python_str)
            .unwrap_or_else(|| format!("region-{}", regions.len() + 1));
        let kind = capture
            .name("kind")
            .map(|m| m.as_str().to_owned())
            .or_else(|| options.get("kind").map(crate::values::python_str))
            .unwrap_or_else(|| "sql".into());
        regions.push(Region {
            id,
            kind,
            options,
            line,
            content: opening.end()..closing.start,
        });
        cursor = closing.end;
    }
    Ok(regions)
}
fn fenced_ranges(text: &str, start: usize) -> Vec<Range<usize>> {
    let mut ranges = Vec::new();
    let mut opened: Option<(u8, usize, usize)> = None;
    let mut offset = start;
    for line in text[start..].split_inclusive('\n') {
        let raw = line.trim_end_matches(['\r', '\n']);
        let indent = raw.bytes().take_while(|b| *b == b' ').count();
        let stripped = if indent <= 3 { &raw[indent..] } else { "" };
        let marker = stripped.as_bytes().first().copied().unwrap_or(0);
        let length = if marker == b'`' || marker == b'~' {
            stripped.bytes().take_while(|b| *b == marker).count()
        } else {
            0
        };
        if let Some((character, minimum, left)) = opened {
            if marker == character && length >= minimum && stripped[length..].trim().is_empty() {
                ranges.push(left..offset + line.len());
                opened = None;
            }
        } else if length >= 3 {
            opened = Some((marker, length, offset));
        }
        offset += line.len();
    }
    if let Some((_, _, left)) = opened {
        ranges.push(left..text.len());
    }
    ranges
}

pub fn source_text(text: &str) -> Result<String> {
    let mut result = text.to_owned();
    for region in parse_regions(text, 0)?.iter().rev() {
        result.replace_range(region.content.clone(), "\n");
    }
    Ok(result)
}

pub(crate) fn replace_regions(
    text: &str,
    replacements: &BTreeMap<String, String>,
) -> Result<String> {
    let mut updated = text.to_owned();
    for region in parse_regions(text, 0)?.iter().rev() {
        if let Some(content) = replacements.get(&region.id) {
            let content = content.trim_end();
            let rendered = if content.is_empty() {
                "\n\n".into()
            } else {
                format!("\n\n{content}\n\n")
            };
            updated.replace_range(region.content.clone(), &rendered);
        }
    }
    Ok(updated)
}

pub(crate) fn pointer_parts(pointer: &str) -> Result<Vec<String>> {
    ensure!(
        pointer.starts_with('/'),
        "front-matter pointer must start with '/': {pointer}"
    );
    Ok(pointer[1..]
        .split('/')
        .map(|part| part.replace("~1", "/").replace("~0", "~"))
        .collect())
}

/// Edit only generated values through a lossless YAML syntax tree.
pub(crate) fn replace_frontmatter(text: &str, patches: &[(String, Value)]) -> Result<String> {
    if patches.is_empty() {
        return Ok(text.into());
    }
    let document = Document::parse(text.into())?;
    let raw = document
        .yaml_range
        .as_ref()
        .map_or("{}\n", |r| &text[r.clone()]);
    // YamlFile, not Document: comments before the first key are stream-level YAML
    // and Document::from_str rejects them rather than round-tripping them.
    let yaml = yaml_edit::YamlFile::from_str(if raw.trim().is_empty() { "{}\n" } else { raw })?;
    let root = yaml
        .ensure_document()
        .as_mapping()
        .context("YAML front matter must be a mapping")?;
    let mut changed = false;
    for (pointer, value) in patches {
        let parts = pointer_parts(pointer)?;
        ensure!(
            parts.len() >= 2 && parts[0] == "generated",
            "frontmatter targets must be under /generated/"
        );
        let existing = Value::Object(document.frontmatter.clone());
        if existing.pointer(pointer) == Some(value) {
            continue;
        }
        let mut cursor = root.clone();
        for part in &parts[..parts.len() - 1] {
            if cursor.get_mapping(part.as_str()).is_none() {
                cursor.set(part.as_str(), yaml_edit::Mapping::new_pending_block());
            }
            cursor = cursor
                .get_mapping(part.as_str())
                .context("failed to create YAML mapping")?;
        }
        let wrapper = serde_norway::to_string(&serde_json::json!({"value":value}))?;
        let new_value = yaml_edit::Document::from_str(&wrapper)?;
        let node = new_value
            .as_mapping()
            .and_then(|m| m.get("value"))
            .context("failed to encode generated value")?;
        cursor.set(parts.last().context("empty pointer")?.as_str(), node);
        changed = true;
    }
    if !changed {
        return Ok(text.into());
    }
    let mut rendered = yaml.to_string();
    if !rendered.ends_with('\n') {
        rendered.push('\n');
    }
    if let Some(range) = document.yaml_range {
        Ok(format!(
            "{}{}{}",
            &text[..range.start],
            rendered,
            &text[range.end..]
        ))
    } else {
        Ok(format!("---\n{rendered}---\n{text}"))
    }
}

pub(crate) fn check_pointer_conflicts(pointers: &[String]) -> Result<()> {
    let decoded: Vec<_> = pointers
        .iter()
        .map(|s| pointer_parts(s))
        .collect::<Result<_>>()?;
    for (index, left) in decoded.iter().enumerate() {
        for right in &decoded[index + 1..] {
            if left.starts_with(right) || right.starts_with(left) {
                bail!("overlapping front-matter targets");
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fences_unicode_and_default_ids() {
        let text = "🦀\n```md\n<!-- insitu:begin include\npath='ignored'\n-->\n<!-- insitu:end -->\n```\n<!-- insitu:begin include\npath='x'\n-->old<!-- insitu:end -->";
        let regions = parse_regions(text, 0).unwrap();
        assert_eq!(regions.len(), 1);
        assert_eq!(regions[0].id, "region-1");
        let mut replacements = BTreeMap::new();
        replacements.insert("region-1".into(), "new".into());
        let changed = replace_regions(text, &replacements).unwrap();
        assert!(changed.starts_with("🦀\n```md\n"));
        assert!(changed.contains("\n\nnew\n\n"));
        assert_eq!(replace_regions(&changed, &replacements).unwrap(), changed);
    }
    #[test]
    fn malformed_marker_is_an_error() {
        assert!(parse_regions("<!-- insitu:begin\nid='x'\n-->", 0).is_err());
    }
    #[test]
    fn source_projection_excludes_owned_fields() {
        let document = Document::parse(
            "---\ntitle: hello\ngenerated: {count: 3}\ninsitu: {}\n---\nauthored\n".into(),
        )
        .unwrap();
        assert_eq!(document.source_frontmatter().len(), 1);
        assert_eq!(document.source_body(), "authored\n");
    }
    #[test]
    fn yaml_edit_preserves_authored_comments_and_quotes() {
        let original = "---\n# leave me\ntitle: 'exact quote'\n---\n\n# body\n";
        let leading = replace_frontmatter(
            "---\n# only a comment\n---\n",
            &[("/generated/a".into(), serde_json::json!(1))],
        )
        .unwrap();
        assert!(leading.starts_with("---\n# only a comment\n"), "{leading}");
        let patches = vec![("/generated/counts".into(), serde_json::json!({"total":2}))];
        let changed = replace_frontmatter(original, &patches).unwrap();
        assert!(changed.contains("# leave me\ntitle: 'exact quote'"));
        assert!(changed.ends_with("---\n\n# body\n"));
        assert_eq!(replace_frontmatter(&changed, &patches).unwrap(), changed);
    }
    #[test]
    fn pointer_overlap_and_escaping() {
        assert!(
            check_pointer_conflicts(&["/generated/a".into(), "/generated/a/b".into()]).is_err()
        );
        assert!(
            check_pointer_conflicts(&["/generated/a~1b".into(), "/generated/a/b".into()]).is_ok()
        );
    }
}
