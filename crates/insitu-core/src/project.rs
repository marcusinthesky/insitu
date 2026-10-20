//! One owner coordinates each pass; only extension RPC is asynchronous.
use crate::{
    ChangeSet, Config, Document, FileChange, Mode,
    builtins::Builtins,
    document,
    extensions::{self, Handler, Process, Registry},
    graph::Graph,
    index::Index,
    paths,
    values::*,
};
use anyhow::{Context, Result, bail, ensure};
use insitu_protocol::{
    CapabilityKind as Kind, CompositionMode, Diagnostic, Incoming, InitializeRequest,
    InitializeResult, InvocationRequest, InvocationResult, Location, Options, Peer,
    ReconcileReport, ResourceRead, ResourceRequest, RpcError, Severity,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    future::Future,
    path::{Path, PathBuf},
    pin::Pin,
    time::{Duration, Instant},
};
use tokio::sync::mpsc;

type LocalFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T>> + 'a>>;

/// A project owns its index, subprocesses and reconciliation state.
/// Call [`Self::close`] before dropping it to allow graceful extension shutdown.
pub struct Project {
    root: PathBuf,
    config: Config,
    mode: Mode,
    index: Index,
    builtins: Builtins,
    registry: Registry,
    processes: Vec<Process>,
    sender: mpsc::UnboundedSender<Incoming>,
    incoming: mpsc::UnboundedReceiver<Incoming>,
    started: bool,
}
impl Project {
    pub fn open(start: impl AsRef<Path>, mode: Mode) -> Result<Self> {
        let root = crate::find_project(start.as_ref())?;
        let config = Config::load(&root)?;
        let index = Index::open(&root, &config, mode)?;
        let builtins = Builtins::new(&root, &config);
        let (sender, incoming) = mpsc::unbounded_channel();
        Ok(Self {
            root,
            config,
            mode,
            index,
            builtins,
            registry: Registry::builtins(),
            processes: vec![],
            sender,
            incoming,
            started: false,
        })
    }
    pub fn root(&self) -> &Path {
        &self.root
    }
    pub fn config(&self) -> &Config {
        &self.config
    }

    /// Scan -> index -> plan -> render -> prepare -> commit. Errors discovered
    /// before commit leave authored files untouched in either mode.
    pub async fn run(&mut self) -> Result<ReconcileReport> {
        let started = Instant::now();
        let documents = self.index.refresh()?;
        self.start().await?;
        let jobs = plan(&documents)?;
        let mut report = ReconcileReport {
            scanned: documents.len(),
            materializations: jobs.len(),
            ..Default::default()
        };
        let mut graph = Graph::default();
        for job in &jobs {
            graph.edge(&job.output(), &job.node())?;
            graph.edge(&format!("fs:file:{}", job.path), &job.output())?;
        }
        for validator in self.registry.validators() {
            match self
                .invoke(
                    Kind::Validator,
                    validator,
                    Options::new(),
                    Location::from_path(""),
                    Value::Null,
                    vec![],
                )
                .await
            {
                Ok(result) => report.diagnostics.extend(result.diagnostics),
                Err(error) => report
                    .diagnostics
                    .push(Diagnostic::error(format!("{error:#}"))),
            }
        }
        let mut rendered = Vec::new();
        if !report.hard_errors() {
            for job in &jobs {
                match self.render_job(job).await.and_then(|result| {
                    for dependency in &result.dependencies {
                        graph.edge(&job.node(), dependency)?;
                    }
                    Ok(result)
                }) {
                    Ok(result) => {
                        report.diagnostics.extend(result.diagnostics.clone());
                        rendered.push((job.clone(), result));
                    }
                    Err(error) => report.diagnostics.push(Diagnostic {
                        severity: Severity::Error,
                        message: format!("{error:#}"),
                        path: Some(job.path.clone()),
                        line: Some(job.line),
                        code: None,
                    }),
                }
            }
        }
        if !report.hard_errors() {
            let changes = prepare(&self.root, &documents, rendered)?;
            report.changed = changes
                .files
                .iter()
                .map(|change| paths::relative_name(&self.root, &change.path))
                .collect::<Result<_>>()?;
            match self.mode {
                Mode::Check => {
                    for path in &report.changed {
                        report.diagnostics.push(Diagnostic {
                            severity: Severity::Error,
                            message: "generated content is stale; run `insitu sync`".into(),
                            path: Some(path.clone()),
                            line: None,
                            code: Some("stale".into()),
                        });
                    }
                }
                Mode::Sync => changes.commit()?,
            }
        }
        report.changed.sort();
        report
            .diagnostics
            .sort_by(|a, b| (&a.path, &a.line, &a.message).cmp(&(&b.path, &b.line, &b.message)));
        report.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        Ok(report)
    }

    async fn start(&mut self) -> Result<()> {
        if self.started {
            return Ok(());
        }
        for package in extensions::discover(&self.root, &self.config)? {
            for dependency in package
                .manifest
                .dependencies
                .iter()
                .filter(|d| d.capability)
            {
                ensure!(
                    self.registry.contains(&dependency.name),
                    "missing capability dependency: {}",
                    dependency.name
                );
            }
            let id = self.processes.len();
            self.processes
                .push(Process::spawn(&package, self.sender.clone(), id)?);
            let initialize = InitializeRequest {
                protocol: 1,
                root: paths::posix(&self.root)?,
                extension: package.manifest.name.clone(),
            };
            let raw = self
                .request(id, "insitu.initialize", serde_json::to_value(initialize)?)
                .await?;
            self.processes[id].verify(&serde_json::from_value::<InitializeResult>(raw)?)?;
            for spec in &package.manifest.capabilities {
                self.registry.register(
                    spec,
                    Handler::External {
                        process: id,
                        name: spec.name.clone(),
                    },
                    &package.allow_replace,
                )?;
            }
        }
        self.registry.validate()?;
        self.started = true;
        Ok(())
    }
    pub async fn close(&mut self) {
        for process in self.processes.iter_mut().rev() {
            process.close().await;
        }
        self.processes.clear();
        self.registry = Registry::builtins();
        self.started = false;
    }

    async fn render_job(&mut self, job: &Job) -> Result<InvocationResult> {
        let location = Location::from_path(&job.path);
        let mut result = if job.pointer.is_some() && job.kind == "sql" {
            let mut result = self.builtins.query(&job.options, &location, &self.index)?;
            if job.options.get("template").is_some_and(truthy) {
                let rendered = self
                    .builtins
                    .render(result.value, &job.options, &location)?;
                result.value = rendered.content.unwrap_or_default().into();
                result.dependencies.extend(rendered.dependencies);
            } else if let Value::Array(rows) = &result.value
                && rows.len() == 1
            {
                // One SQL row is an object, as in Python; zero or many stay an array.
                result.value = rows[0].clone();
            }
            result
        } else if job.pointer.is_some() && job.options.get("source").is_some_and(truthy) {
            let source = option_string(&job.options, "source", "");
            self.invoke(
                Kind::Resource,
                source,
                job.options.clone(),
                location,
                Value::Null,
                vec![],
            )
            .await?
        } else {
            let mut result = self
                .invoke(
                    Kind::Transform,
                    job.kind.clone(),
                    job.options.clone(),
                    location,
                    Value::Null,
                    vec![],
                )
                .await?;
            if job.pointer.is_some() {
                result.value = result.content.clone().unwrap_or_default().into();
            }
            result
        };
        result
            .dependencies
            .push(format!("markdown:{}#directive:{}", job.path, job.id));
        deduplicate(&mut result.dependencies);
        Ok(result)
    }

    // Boxing is confined to recursive capability/resource dispatch; the rest of
    // the application exposes concrete types and ordinary async functions.
    fn invoke(
        &mut self,
        kind: Kind,
        name: String,
        options: Options,
        location: Location,
        value: Value,
        mut trace: Vec<String>,
    ) -> LocalFuture<'_, InvocationResult> {
        Box::pin(async move {
            let node = format!("{}:{name}", kind.method().trim_start_matches("insitu."));
            ensure!(
                !trace.contains(&node),
                "dependency cycle: {} -> {node}",
                trace.join(" -> ")
            );
            trace.push(node);
            let mut output = InvocationResult::default();
            for step in self.registry.steps(kind, &name)? {
                let mut args = options.clone();
                if step.mode == CompositionMode::Decorate {
                    args.insert(
                        "content".into(),
                        output.content.clone().unwrap_or_default().into(),
                    );
                }
                let result = self
                    .execute(
                        step.handler,
                        kind,
                        args,
                        location.clone(),
                        value.clone(),
                        trace.clone(),
                    )
                    .await?;
                if step.mode == CompositionMode::Contribute {
                    output
                        .content
                        .get_or_insert_with(String::new)
                        .push_str(result.content.as_deref().unwrap_or(""));
                } else {
                    output.content = result.content;
                    output.value = result.value;
                }
                output.dependencies.extend(result.dependencies);
                output.diagnostics.extend(result.diagnostics);
            }
            deduplicate(&mut output.dependencies);
            Ok(output)
        })
    }
    fn execute(
        &mut self,
        handler: Handler,
        kind: Kind,
        options: Options,
        location: Location,
        value: Value,
        mut trace: Vec<String>,
    ) -> LocalFuture<'_, InvocationResult> {
        Box::pin(async move {
            match handler {
                Handler::External { process, name } => {
                    let node = format!("{}:{name}", kind.method().trim_start_matches("insitu."));
                    if trace.last() != Some(&node) {
                        ensure!(
                            !trace.contains(&node),
                            "dependency cycle: {} -> {node}",
                            trace.join(" -> ")
                        );
                        trace.push(node);
                    }
                    let request = InvocationRequest {
                        name,
                        options,
                        location,
                        value,
                        trace,
                    };
                    let raw = self
                        .request(process, kind.method(), serde_json::to_value(request)?)
                        .await?;
                    Ok(serde_json::from_value(raw)?)
                }
                Handler::Builtin(Kind::Resource, name) if name == "sql" => {
                    self.builtins.query(&options, &location, &self.index)
                }
                Handler::Builtin(Kind::Renderer, name) if name == "jinja" => {
                    self.builtins.render(value, &options, &location)
                }
                Handler::Builtin(Kind::Transform, name) if name == "include" => {
                    self.builtins.include(&options, &location)
                }
                Handler::Builtin(Kind::Transform, name) if name == "tree" => {
                    self.builtins.tree(&options, &location, &self.index.policy)
                }
                Handler::Builtin(Kind::Transform, name)
                    if name == "sql" || name == "materialize" =>
                {
                    let (source, renderer) = if name == "sql" {
                        ("sql".into(), "jinja".into())
                    } else {
                        (
                            option_string(&options, "source", ""),
                            option_string(&options, "renderer", "jinja"),
                        )
                    };
                    ensure!(!source.is_empty(), "materialize transform requires source");
                    let resource = self
                        .invoke(
                            Kind::Resource,
                            source,
                            options.clone(),
                            location.clone(),
                            Value::Null,
                            trace.clone(),
                        )
                        .await?;
                    let mut rendered = self
                        .invoke(
                            Kind::Renderer,
                            renderer,
                            options,
                            location,
                            resource.value,
                            trace,
                        )
                        .await?;
                    rendered.dependencies.extend(resource.dependencies);
                    rendered.diagnostics.extend(resource.diagnostics);
                    Ok(rendered)
                }
                Handler::Builtin(_, name) => bail!("unknown built-in capability: {name}"),
            }
        })
    }

    async fn request(&mut self, process: usize, method: &str, params: Value) -> Result<Value> {
        let peer = self
            .processes
            .get(process)
            .context("unknown extension process")?
            .peer
            .clone();
        let response = peer.request(method, params, Duration::from_secs(30));
        tokio::pin!(response);
        loop {
            tokio::select! {
                result = &mut response => return Ok(result?),
                incoming = self.incoming.recv() => {
                    let incoming = incoming.context("extension callback channel closed")?;
                    self.dispatch(incoming).await?;
                }
            }
        }
    }
    async fn dispatch(&mut self, incoming: Incoming) -> Result<()> {
        let process = self
            .processes
            .get(incoming.source)
            .context("callback from unknown process")?;
        let peer: Peer = process.peer.clone();
        let permissions = process.manifest.permissions.clone();
        let result = if incoming.method != "host.resource.get" {
            Err(RpcError::Remote {
                code: -32601,
                message: format!("unknown method: {}", incoming.method),
            })
        } else {
            let result = async {
                let request: ResourceRequest = serde_json::from_value(incoming.params)?;
                ensure!(
                    extensions::resource_allowed(&request.resource, &permissions),
                    "extension may not read resource {}",
                    request.resource
                );
                let read = self.resource(request).await?;
                Ok::<_, anyhow::Error>(serde_json::to_value(read)?)
            }
            .await;
            result.map_err(|error| RpcError::Remote {
                code: -32000,
                message: format!("{error:#}"),
            })
        };
        peer.respond(incoming.id, result).await?;
        Ok(())
    }
    fn resource(&mut self, request: ResourceRequest) -> LocalFuture<'_, ResourceRead> {
        Box::pin(async move {
            let ResourceRequest {
                resource,
                location,
                mut trace,
            } = request;
            ensure!(
                !trace.contains(&resource),
                "dependency cycle: {} -> {resource}",
                trace.join(" -> ")
            );
            trace.push(resource.clone());
            if resource.starts_with("git://") {
                return Ok(ResourceRead {
                    value: self.index.policy.git.resource(&resource)?,
                    dependencies: vec![resource],
                });
            }
            if let Some(raw) = resource.strip_prefix("fs:file:") {
                let path = paths::inside(&self.root, raw)?;
                let text = fs::read_to_string(&path)?;
                let text = if path
                    .extension()
                    .is_some_and(|ext| ext.to_str().is_some_and(|s| s.eq_ignore_ascii_case("md")))
                {
                    document::source_text(&text)?
                } else {
                    text
                };
                return Ok(ResourceRead {
                    value: text.into(),
                    dependencies: vec![resource],
                });
            }
            if let Some(pattern) = resource.strip_prefix("fs:glob:") {
                let mut include = ignore::gitignore::GitignoreBuilder::new(&self.root);
                include.add_line(None, pattern)?;
                let include = include.build()?;
                let mut exclude = ignore::gitignore::GitignoreBuilder::new(&self.root);
                for pattern in &self.config.sources.exclude {
                    exclude.add_line(None, pattern)?;
                }
                let exclude = exclude.build()?;
                let mut matches = Vec::new();
                for entry in walkdir::WalkDir::new(&self.root).follow_links(false) {
                    let entry = entry?;
                    if entry.file_type().is_file()
                        && include.matched(entry.path(), false).is_ignore()
                        && !exclude
                            .matched_path_or_any_parents(entry.path(), false)
                            .is_ignore()
                    {
                        matches.push(paths::relative_name(&self.root, entry.path())?);
                    }
                }
                matches.sort();
                return Ok(ResourceRead {
                    value: json!(matches),
                    dependencies: vec![resource],
                });
            }
            for (prefix, sql) in [("sql:model:", true), ("resource:", false)] {
                if let Some(raw) = resource.strip_prefix(prefix) {
                    let (name, query) = raw.split_once('?').unwrap_or((raw, ""));
                    let options: Options = url::form_urlencoded::parse(query.as_bytes())
                        .map(|(key, value)| (key.into_owned(), Value::String(value.into_owned())))
                        .collect();
                    let mut result = if sql {
                        let options = json!({"model":name,"params":options})
                            .as_object()
                            .cloned()
                            .context("invalid model options")?;
                        self.builtins.query(&options, &location, &self.index)?
                    } else {
                        self.invoke(
                            Kind::Resource,
                            name.into(),
                            options,
                            location,
                            Value::Null,
                            trace,
                        )
                        .await?
                    };
                    result.dependencies.insert(0, resource);
                    deduplicate(&mut result.dependencies);
                    return Ok(ResourceRead {
                        value: result.value,
                        dependencies: result.dependencies,
                    });
                }
            }
            bail!("unsupported host resource: {resource}")
        })
    }
}

#[derive(Clone)]
struct Job {
    path: String,
    id: String,
    kind: String,
    options: Options,
    line: usize,
    pointer: Option<String>,
}
impl Job {
    fn node(&self) -> String {
        format!("materialization:{}#{}", self.path, self.id)
    }
    fn output(&self) -> String {
        match &self.pointer {
            Some(pointer) => format!("yaml:{}#{pointer}", self.path),
            None => format!("markdown:{}#region:{}", self.path, self.id),
        }
    }
}
fn target(options: &Options) -> Result<Option<String>> {
    let raw = options.get("target");
    let pointer: Option<String> = match raw {
        Some(Value::String(s)) if s.starts_with("frontmatter:") => {
            Some(s.trim_start_matches("frontmatter:").into())
        }
        Some(Value::String(s)) if s == "frontmatter" => {
            bail!("frontmatter target requires pointer")
        }
        Some(Value::Object(map))
            if map.get("kind").and_then(Value::as_str) == Some("frontmatter") =>
        {
            Some(
                map.get("pointer")
                    .and_then(Value::as_str)
                    .context("frontmatter target requires pointer")?
                    .into(),
            )
        }
        _ => None,
    };
    if let Some(pointer) = &pointer {
        ensure!(
            pointer.starts_with("/generated/"),
            "frontmatter targets must be under /generated/"
        );
    }
    Ok(pointer)
}
fn plan(documents: &[(String, Document)]) -> Result<Vec<Job>> {
    let mut jobs = Vec::new();
    for (path, document) in documents {
        let mut identities = BTreeSet::new();
        let mut pointers = Vec::new();
        let definitions = document
            .regions
            .iter()
            .map(|region| {
                (
                    region.id.clone(),
                    region.kind.clone(),
                    region.options.clone(),
                    region.line,
                )
            })
            .chain(
                document
                    .materializations()
                    .into_iter()
                    .map(|(id, options)| {
                        let kind = option_string(&options, "kind", "sql");
                        (id, kind, options, 1)
                    }),
            );
        for (id, kind, options, line) in definitions {
            ensure!(
                identities.insert(id.clone()),
                "multiple materializations own {path}#{id}"
            );
            let pointer = target(&options)?;
            if let Some(pointer) = &pointer {
                pointers.push(pointer.clone());
            }
            jobs.push(Job {
                path: path.clone(),
                id,
                kind,
                options,
                line,
                pointer,
            });
        }
        document::check_pointer_conflicts(&pointers)?;
    }
    Ok(jobs)
}
fn prepare(
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
            match job.pointer {
                Some(pointer) => frontmatter.push((pointer, result.value)),
                None => {
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
fn deduplicate(values: &mut Vec<String>) {
    let mut seen = BTreeSet::new();
    values.retain(|value| seen.insert(value.clone()));
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        crate::initialize_project(dir.path(), false).unwrap();
        fs::write(dir.path().join("snippet.txt"), "hello\n").unwrap();
        fs::write(dir.path().join("notes.md"), "# Notes\n\n<!-- insitu:begin include\nid='example'\npath='snippet.txt'\n-->\nstale\n<!-- insitu:end -->\n").unwrap();
        dir
    }
    #[tokio::test]
    async fn sync_check_and_idempotence() {
        let dir = fixture();
        let mut check = Project::open(dir.path(), Mode::Check).unwrap();
        assert_eq!(check.run().await.unwrap().changed, ["notes.md"]);
        check.close().await;
        let mut sync = Project::open(dir.path(), Mode::Sync).unwrap();
        assert!(sync.run().await.unwrap().ok());
        assert!(sync.run().await.unwrap().changed.is_empty());
        sync.close().await;
        let mut check = Project::open(dir.path(), Mode::Check).unwrap();
        assert!(check.run().await.unwrap().ok());
        check.close().await;
    }
    #[tokio::test]
    async fn self_include_never_writes() {
        let dir = fixture();
        let path = dir.path().join("notes.md");
        let original = fs::read_to_string(&path)
            .unwrap()
            .replace("snippet.txt", "notes.md");
        fs::write(&path, &original).unwrap();
        let mut project = Project::open(dir.path(), Mode::Sync).unwrap();
        let report = project.run().await.unwrap();
        project.close().await;
        assert!(report.hard_errors());
        assert_eq!(fs::read_to_string(path).unwrap(), original);
    }
}
