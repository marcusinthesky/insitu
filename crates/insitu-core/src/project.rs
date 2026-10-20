//! One owner coordinates each pass; only extension RPC is asynchronous.
use crate::{
    builtins::{Builtin, Builtins},
    config::Config,
    document,
    extensions::{self, Handler, Process, Registry},
    graph::Graph,
    index::Index,
    patch::{ChangeSet, Mode},
    paths,
    plan::{Job, Target, plan, prepare},
    policy::Policy,
    values::{option_string, truthy},
};
use anyhow::{Context, Result, bail, ensure};
use ignore::gitignore::GitignoreBuilder;
use insitu_protocol::{
    CapabilityKind, CompositionMode, Diagnostic, Incoming, InitializeRequest, InitializeResult,
    InvocationRequest, InvocationResult, Location, Options, ReconcileReport, ResourceRead,
    ResourceRequest, RpcError,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use tokio::sync::mpsc;

/// A project owns its index, subprocesses and reconciliation state.
/// Call [`Self::close`] before dropping it to allow graceful extension shutdown.
pub struct Project {
    root: PathBuf,
    config: Config,
    mode: Mode,
    policy: Policy,
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
        let policy = Policy::new(&root, &config)?;
        let index = Index::open(&root, &config, mode)?;
        let builtins = Builtins::new(&root, config.paths.clone());
        let (sender, incoming) = mpsc::unbounded_channel();
        Ok(Self {
            root,
            config,
            mode,
            policy,
            index,
            builtins,
            registry: Registry::builtins(),
            processes: Vec::new(),
            sender,
            incoming,
            started: false,
        })
    }

    /// How long `watch` gathers filesystem events before starting a pass.
    pub fn debounce(&self) -> Duration {
        Duration::from_millis(self.config.watch.debounce_ms)
    }

    /// Scan -> index -> plan -> render -> prepare -> commit. Errors discovered
    /// before commit leave authored files untouched in either mode.
    pub async fn run(&mut self) -> Result<ReconcileReport> {
        let started = Instant::now();
        let documents = self.index.refresh(&self.policy)?;
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
        self.validate(&mut report).await;
        if !report.hard_errors() {
            let rendered = self.render_all(&jobs, &mut graph, &mut report).await;
            if !report.hard_errors() {
                let changes = prepare(&self.root, &documents, rendered)?;
                self.apply(changes, &mut report)?;
            }
        }
        report.changed.sort();
        report
            .diagnostics
            .sort_by(|a, b| (&a.path, &a.line, &a.message).cmp(&(&b.path, &b.line, &b.message)));
        report.elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        Ok(report)
    }

    /// Run every validator, reporting failures as diagnostics.
    async fn validate(&mut self, report: &mut ReconcileReport) {
        for validator in self.registry.validators() {
            let call = InvocationRequest::new(validator, Options::new(), Location::from_path(""));
            match self.invoke(CapabilityKind::Validator, call).await {
                Ok(result) => report.diagnostics.extend(result.diagnostics),
                Err(error) => report
                    .diagnostics
                    .push(Diagnostic::error(format!("{error:#}"))),
            }
        }
    }

    /// Render every job, reporting failures as diagnostics located at the job.
    async fn render_all(
        &mut self,
        jobs: &[Job],
        graph: &mut Graph,
        report: &mut ReconcileReport,
    ) -> Vec<(Job, InvocationResult)> {
        let mut rendered = Vec::new();
        for job in jobs {
            match self.render_job(job, graph).await {
                Ok(mut result) => {
                    report.diagnostics.append(&mut result.diagnostics);
                    rendered.push((job.clone(), result));
                }
                Err(error) => report.diagnostics.push(Diagnostic {
                    path: Some(job.path.clone()),
                    line: Some(job.line),
                    ..Diagnostic::error(format!("{error:#}"))
                }),
            }
        }
        rendered
    }

    /// Record the changed files, then write them (sync) or flag them stale (check).
    fn apply(&self, changes: ChangeSet, report: &mut ReconcileReport) -> Result<()> {
        report.changed = changes
            .files
            .iter()
            .map(|change| paths::relative_name(&self.root, &change.path))
            .collect::<Result<_>>()?;
        match self.mode {
            Mode::Check => {
                for path in &report.changed {
                    report.diagnostics.push(Diagnostic {
                        path: Some(path.clone()),
                        code: Some("stale".into()),
                        ..Diagnostic::error("generated content is stale; run `insitu sync`")
                    });
                }
            }
            Mode::Sync => changes.commit()?,
        }
        Ok(())
    }

    /// Start every configured extension once. If any fails, stop those already
    /// running so that the next pass starts clean.
    async fn start(&mut self) -> Result<()> {
        if self.started {
            return Ok(());
        }
        if let Err(error) = self.start_extensions().await {
            self.close().await;
            return Err(error);
        }
        self.started = true;
        Ok(())
    }

    async fn start_extensions(&mut self) -> Result<()> {
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
        self.registry.validate()
    }

    /// Shut extensions down, newest first, and forget their registrations.
    pub async fn close(&mut self) {
        for process in self.processes.iter_mut().rev() {
            process.close().await;
        }
        self.processes.clear();
        self.registry = Registry::builtins();
        self.started = false;
    }

    /// Render one job and record its dependencies in the ownership graph.
    async fn render_job(&mut self, job: &Job, graph: &mut Graph) -> Result<InvocationResult> {
        let location = Location::from_path(&job.path);
        let mut result = match &job.target {
            Target::Frontmatter(_) if job.kind == "sql" => {
                self.query_frontmatter(&job.options, &location)?
            }
            Target::Frontmatter(_) if job.options.get("source").is_some_and(truthy) => {
                let source = option_string(&job.options, "source", "");
                let call = InvocationRequest::new(source, job.options.clone(), location);
                self.invoke(CapabilityKind::Resource, call).await?
            }
            target => {
                let call = InvocationRequest::new(job.kind.as_str(), job.options.clone(), location);
                let mut result = self.invoke(CapabilityKind::Transform, call).await?;
                if let Target::Frontmatter(_) = target {
                    result.value = result.content.clone().unwrap_or_default().into();
                }
                result
            }
        };
        result
            .dependencies
            .push(format!("markdown:{}#directive:{}", job.path, job.id));
        deduplicate(&mut result.dependencies);
        for dependency in &result.dependencies {
            graph.edge(&job.node(), dependency)?;
        }
        Ok(result)
    }

    /// A front-matter SQL value: rendered through a template when one is set;
    /// otherwise a single row is an object, as in Python, and other counts an array.
    fn query_frontmatter(
        &self,
        options: &Options,
        location: &Location,
    ) -> Result<InvocationResult> {
        let mut result = self.builtins.query(options, location, &self.index)?;
        if options.get("template").is_some_and(truthy) {
            let rendered = self.builtins.render(result.value, options, location)?;
            result.value = rendered.content.unwrap_or_default().into();
            result.dependencies.extend(rendered.dependencies);
        } else if let Value::Array(rows) = &result.value
            && rows.len() == 1
        {
            result.value = rows[0].clone();
        }
        Ok(result)
    }

    /// Run a capability's composed steps: its provider, then decorators in
    /// order, then contributors.
    async fn invoke(
        &mut self,
        kind: CapabilityKind,
        mut call: InvocationRequest,
    ) -> Result<InvocationResult> {
        enter(&mut call.trace, trace_node(kind, &call.name))?;
        let mut output = InvocationResult::default();
        for step in self.registry.steps(kind, &call.name)? {
            let mut step_call = call.clone();
            if step.mode == CompositionMode::Decorate {
                step_call.options.insert(
                    "content".into(),
                    output.content.clone().unwrap_or_default().into(),
                );
            }
            let result = self.execute(step.handler, kind, step_call).await?;
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
    }

    /// Run one step: a request to an extension process, or a built-in.
    async fn execute(
        &mut self,
        handler: Handler,
        kind: CapabilityKind,
        mut call: InvocationRequest,
    ) -> Result<InvocationResult> {
        match handler {
            Handler::External { process, name } => {
                let node = trace_node(kind, &name);
                if call.trace.last() != Some(&node) {
                    enter(&mut call.trace, node)?;
                }
                call.name = name;
                let raw = self
                    .request(process, kind.method(), serde_json::to_value(call)?)
                    .await?;
                Ok(serde_json::from_value(raw)?)
            }
            Handler::Builtin(Builtin::SqlResource) => {
                self.builtins
                    .query(&call.options, &call.location, &self.index)
            }
            Handler::Builtin(Builtin::Jinja) => {
                self.builtins
                    .render(call.value, &call.options, &call.location)
            }
            Handler::Builtin(Builtin::Include) => {
                self.builtins.include(&call.options, &call.location)
            }
            Handler::Builtin(Builtin::Tree) => {
                self.builtins
                    .tree(&call.options, &call.location, &self.policy)
            }
            Handler::Builtin(Builtin::SqlTransform) => {
                self.materialize("sql".into(), "jinja".into(), call).await
            }
            Handler::Builtin(Builtin::Materialize) => {
                let source = option_string(&call.options, "source", "");
                let renderer = option_string(&call.options, "renderer", "jinja");
                self.materialize(source, renderer, call).await
            }
        }
    }

    /// Read a resource, then render its value: the `sql` and `materialize` transforms.
    async fn materialize(
        &mut self,
        source: String,
        renderer: String,
        call: InvocationRequest,
    ) -> Result<InvocationResult> {
        ensure!(!source.is_empty(), "materialize transform requires source");
        // Calls back into `invoke` are boxed, here and in `resource`: a future
        // cannot contain itself.
        let resource_call = InvocationRequest {
            name: source,
            value: Value::Null,
            ..call.clone()
        };
        let resource = Box::pin(self.invoke(CapabilityKind::Resource, resource_call)).await?;
        let renderer_call = InvocationRequest {
            name: renderer,
            value: resource.value,
            ..call
        };
        let mut rendered = Box::pin(self.invoke(CapabilityKind::Renderer, renderer_call)).await?;
        rendered.dependencies.extend(resource.dependencies);
        rendered.diagnostics.extend(resource.diagnostics);
        Ok(rendered)
    }

    /// Send a request to an extension, answering its callbacks while waiting.
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

    /// Answer an extension's callback; `host.resource.get` is the only method.
    async fn dispatch(&mut self, incoming: Incoming) -> Result<()> {
        let process = self
            .processes
            .get(incoming.source)
            .context("callback from unknown process")?;
        let peer = process.peer.clone();
        let permissions = process.manifest.permissions.clone();
        let result = if incoming.method == "host.resource.get" {
            self.read_resource(incoming.params, &permissions)
                .await
                .map_err(|error| RpcError::Remote {
                    code: RpcError::SERVER_ERROR,
                    message: format!("{error:#}"),
                })
        } else {
            Err(RpcError::Remote {
                code: RpcError::METHOD_NOT_FOUND,
                message: format!("unknown method: {}", incoming.method),
            })
        };
        peer.respond(incoming.id, result).await?;
        Ok(())
    }

    async fn read_resource(&mut self, params: Value, permissions: &[String]) -> Result<Value> {
        let request: ResourceRequest = serde_json::from_value(params)?;
        ensure!(
            extensions::resource_allowed(&request.resource, permissions),
            "extension may not read resource {}",
            request.resource
        );
        Ok(serde_json::to_value(self.resource(request).await?)?)
    }

    /// Resolve a host resource identifier to its value and dependencies.
    async fn resource(&mut self, request: ResourceRequest) -> Result<ResourceRead> {
        let ResourceRequest {
            resource,
            location,
            mut trace,
        } = request;
        enter(&mut trace, resource.clone())?;
        let (value, mut dependencies) = if resource.starts_with("git://") {
            (self.policy.git.resource(&resource)?, Vec::new())
        } else if let Some(path) = resource.strip_prefix("fs:file:") {
            (Value::String(self.read_source(path)?), Vec::new())
        } else if let Some(pattern) = resource.strip_prefix("fs:glob:") {
            (json!(self.glob(pattern)?), Vec::new())
        } else if let Some(query) = resource.strip_prefix("sql:model:") {
            let (model, params) = query_options(query);
            let options = Options::from_iter([
                ("model".to_owned(), Value::from(model)),
                ("params".to_owned(), Value::Object(params)),
            ]);
            let result = self.builtins.query(&options, &location, &self.index)?;
            (result.value, result.dependencies)
        } else if let Some(query) = resource.strip_prefix("resource:") {
            let (name, options) = query_options(query);
            let call = InvocationRequest {
                trace,
                ..InvocationRequest::new(name, options, location)
            };
            let result = Box::pin(self.invoke(CapabilityKind::Resource, call)).await?;
            (result.value, result.dependencies)
        } else {
            bail!("unsupported host resource: {resource}");
        };
        dependencies.insert(0, resource);
        deduplicate(&mut dependencies);
        Ok(ResourceRead {
            value,
            dependencies,
        })
    }

    /// A project file's text; Markdown loses its generated region bodies.
    fn read_source(&self, raw: &str) -> Result<String> {
        let path = paths::inside(&self.root, raw)?;
        let text = fs::read_to_string(&path)?;
        if paths::is_markdown(&path) {
            document::source_text(&text)
        } else {
            Ok(text)
        }
    }

    /// Project files matching a gitignore-style pattern, minus `sources.exclude`.
    fn glob(&self, pattern: &str) -> Result<Vec<String>> {
        let mut include = GitignoreBuilder::new(&self.root);
        include.add_line(None, pattern)?;
        let include = include.build()?;
        let mut exclude = GitignoreBuilder::new(&self.root);
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
        Ok(matches)
    }
}

/// Split `name?key=value&…` into the name and its URL-encoded string options.
fn query_options(raw: &str) -> (&str, Options) {
    let (name, query) = raw.split_once('?').unwrap_or((raw, ""));
    let options = url::form_urlencoded::parse(query.as_bytes())
        .map(|(key, value)| (key.into_owned(), Value::String(value.into_owned())))
        .collect();
    (name, options)
}

/// Append `node` to an invocation trace, failing if it is already there.
fn enter(trace: &mut Vec<String>, node: String) -> Result<()> {
    ensure!(
        !trace.contains(&node),
        "dependency cycle: {} -> {node}",
        trace.join(" -> ")
    );
    trace.push(node);
    Ok(())
}

/// A trace entry such as `transform:include` or `render:jinja`.
fn trace_node(kind: CapabilityKind, name: &str) -> String {
    let method = kind.method();
    format!(
        "{}:{name}",
        method.strip_prefix("insitu.").unwrap_or(method)
    )
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
