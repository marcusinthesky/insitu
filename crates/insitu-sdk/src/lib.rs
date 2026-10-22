//! Typed, asynchronous authoring of v1 process extensions.
//!
//! ```no_run
//! use insitu_sdk::{Context, Extension, Options};
//! # async fn example() -> anyhow::Result<()> {
//! let mut app = Extension::new("greeting", "1.0.0");
//! app.transform("hello", |options: Options, _ctx: Context| async move {
//!     Ok(format!("Hello, {}!", options.get("name").and_then(|v| v.as_str()).unwrap_or("world")))
//! })?;
//! app.run().await
//! # }
//! ```
use anyhow::{Context as _, Result, ensure};
use insitu_protocol::{
    CapabilityKind, InitializeRequest, InitializeResult, InvocationRequest, InvocationResult, Peer,
    ResourceRead, ResourceRequest, RpcError,
};
pub use insitu_protocol::{Diagnostic, Location, Options, Severity};
use schemars::JsonSchema;
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    path::{Path, PathBuf},
    pin::Pin,
    sync::{Arc, Mutex, PoisonError},
    time::Duration,
};
use tokio::{sync::mpsc, task::JoinSet};

/// Invocation-local state. Reads record the host's flattened dependencies.
#[derive(Clone)]
pub struct Context {
    pub root: PathBuf,
    pub location: Location,
    peer: Peer,
    trace: Vec<String>,
    dependencies: Arc<Mutex<Vec<String>>>,
}

impl Context {
    /// Read a host resource such as `fs:file:README.md`; the extension's
    /// manifest must grant a matching permission.
    pub async fn resource(&self, resource: impl Into<String>) -> Result<Value> {
        let request = ResourceRequest {
            resource: resource.into(),
            location: self.location.clone(),
            trace: self.trace.clone(),
        };
        let value = self
            .peer
            .request(
                "host.resource.get",
                serde_json::to_value(request)?,
                Duration::from_secs(30),
            )
            .await?;
        let read: ResourceRead = serde_json::from_value(value)?;
        self.dependencies
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .extend(read.dependencies);
        Ok(read.value)
    }
}

type HandlerFuture = Pin<Box<dyn Future<Output = Result<InvocationResult>> + Send>>;
type Handler = dyn Fn(InvocationRequest, Context) -> HandlerFuture + Send + Sync;

struct Registration {
    schema: Value,
    handler: Box<Handler>,
}

/// A small capability registry; the transport is shared with the Rust host.
pub struct Extension {
    name: String,
    version: String,
    registrations: BTreeMap<(CapabilityKind, String), Arc<Registration>>,
}

impl Extension {
    pub fn new(name: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            version: version.into(),
            registrations: BTreeMap::new(),
        }
    }

    /// Register a transform: typed parameters in, Markdown content out.
    pub fn transform<P, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        P: DeserializeOwned + JsonSchema + 'static,
        F: Fn(P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<String>> + Send + 'static,
    {
        let schema = json!({"params": schema::<P>()?, "result": schema::<String>()?});
        self.insert(
            CapabilityKind::Transform,
            name,
            schema,
            move |request, context| {
                let output = params::<P>(request.options).map(|params| handler(params, context));
                Box::pin(async move {
                    Ok(InvocationResult {
                        content: Some(output?.await?),
                        ..Default::default()
                    })
                })
            },
        )
    }

    /// Register a resource: typed parameters in, any serializable value out.
    pub fn resource<P, R, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        P: DeserializeOwned + JsonSchema + 'static,
        R: Serialize + JsonSchema + 'static,
        F: Fn(P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<R>> + Send + 'static,
    {
        let schema = json!({"params": schema::<P>()?, "result": schema::<R>()?});
        self.insert(
            CapabilityKind::Resource,
            name,
            schema,
            move |request, context| {
                let output = params::<P>(request.options).map(|params| handler(params, context));
                Box::pin(async move {
                    Ok(InvocationResult {
                        value: serde_json::to_value(output?.await?)?,
                        ..Default::default()
                    })
                })
            },
        )
    }

    /// Register a renderer: a typed value and parameters in, Markdown content out.
    pub fn renderer<V, P, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        V: DeserializeOwned + JsonSchema + 'static,
        P: DeserializeOwned + JsonSchema + 'static,
        F: Fn(V, P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<String>> + Send + 'static,
    {
        let schema = json!({
            "value": schema::<V>()?,
            "params": schema::<P>()?,
            "result": schema::<String>()?,
        });
        self.insert(
            CapabilityKind::Renderer,
            name,
            schema,
            move |request, context| {
                let output = serde_json::from_value::<V>(request.value)
                    .and_then(|value| Ok(handler(value, params::<P>(request.options)?, context)));
                Box::pin(async move {
                    Ok(InvocationResult {
                        content: Some(output?.await?),
                        ..Default::default()
                    })
                })
            },
        )
    }

    /// Register a validator: typed parameters in, diagnostics out.
    pub fn validator<P, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        P: DeserializeOwned + JsonSchema + 'static,
        F: Fn(P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Vec<Diagnostic>>> + Send + 'static,
    {
        let schema = json!({"params": schema::<P>()?, "result": schema::<Vec<Diagnostic>>()?});
        self.insert(
            CapabilityKind::Validator,
            name,
            schema,
            move |request, context| {
                let output = params::<P>(request.options).map(|params| handler(params, context));
                Box::pin(async move {
                    Ok(InvocationResult {
                        diagnostics: output?.await?,
                        ..Default::default()
                    })
                })
            },
        )
    }

    fn insert(
        &mut self,
        kind: CapabilityKind,
        name: &str,
        schema: Value,
        handler: impl Fn(InvocationRequest, Context) -> HandlerFuture + Send + Sync + 'static,
    ) -> Result<&mut Self> {
        ensure!(!name.is_empty(), "capability name cannot be empty");
        let key = (kind, name.into());
        ensure!(
            !self.registrations.contains_key(&key),
            "duplicate {}: {name}",
            kind.as_str()
        );
        self.registrations.insert(
            key,
            Arc::new(Registration {
                schema,
                handler: Box::new(handler),
            }),
        );
        Ok(self)
    }

    /// Serve LSP-framed JSON-RPC on stdin/stdout. Log only to stderr.
    pub async fn run(self) -> Result<()> {
        ensure!(
            !self.name.is_empty() && !self.version.is_empty(),
            "extension identity cannot be empty"
        );
        let (sender, mut incoming) = mpsc::unbounded_channel();
        let peer = Peer::connect(tokio::io::stdin(), tokio::io::stdout(), sender, 0);
        let mut root = None;
        let mut tasks = JoinSet::new();
        while let Some(call) = incoming.recv().await {
            match call.method.as_str() {
                "insitu.initialize" => {
                    let result = self.initialize(call.params).map(|(directory, result)| {
                        root = Some(directory);
                        result
                    });
                    peer.respond(call.id, result.map_err(rpc_error)).await?;
                }
                "insitu.shutdown" => {
                    peer.respond(call.id, Ok(Value::Null)).await?;
                    break;
                }
                method => match self.prepare(method, call.params, root.as_deref(), &peer) {
                    Ok((registration, request, context)) => {
                        tasks.spawn(serve(peer.clone(), call.id, registration, request, context));
                    }
                    Err(error) => peer.respond(call.id, Err(rpc_error(error))).await?,
                },
            }
            while tasks.try_join_next().is_some() {}
        }
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
        peer.close().await;
        Ok(())
    }

    /// Answer `insitu.initialize` with this extension's identity and schemas,
    /// returning the canonical project root alongside the result.
    fn initialize(&self, params: Value) -> Result<(PathBuf, Value)> {
        let request: InitializeRequest = serde_json::from_value(params)?;
        let root = PathBuf::from(request.root).canonicalize()?;
        let result = InitializeResult {
            name: self.name.clone(),
            version: self.version.clone(),
            protocol: 1,
            capabilities: self.capabilities(),
        };
        Ok((root, serde_json::to_value(result)?))
    }

    /// Registered schemas, keyed by capability kind and then by name.
    fn capabilities(&self) -> BTreeMap<String, Value> {
        CapabilityKind::ALL
            .into_iter()
            .map(|kind| {
                let schemas = self
                    .registrations
                    .iter()
                    .filter(|((registered, _), _)| *registered == kind)
                    .map(|((_, name), registration)| (name.clone(), registration.schema.clone()))
                    .collect();
                (kind.as_str().to_owned(), Value::Object(schemas))
            })
            .collect()
    }

    /// Decode a capability call into its registration, request and context.
    fn prepare(
        &self,
        method: &str,
        params: Value,
        root: Option<&Path>,
        peer: &Peer,
    ) -> Result<(Arc<Registration>, InvocationRequest, Context)> {
        let root = root.context("extension has not been initialized")?;
        let kind = CapabilityKind::from_method(method).context("unknown method")?;
        let request: InvocationRequest = serde_json::from_value(params)?;
        let registration = self
            .registrations
            .get(&(kind, request.name.clone()))
            .context("unknown capability")?;
        let context = Context {
            root: root.to_owned(),
            location: request.location.clone(),
            peer: peer.clone(),
            trace: request.trace.clone(),
            dependencies: Arc::default(),
        };
        Ok((Arc::clone(registration), request, context))
    }
}

/// Run one handler and reply with its result and the dependencies it read.
async fn serve(
    peer: Peer,
    id: Option<Value>,
    registration: Arc<Registration>,
    request: InvocationRequest,
    context: Context,
) {
    let observed = Arc::clone(&context.dependencies);
    let result = (registration.handler)(request, context)
        .await
        .and_then(|mut result| {
            let mut seen = BTreeSet::new();
            result.dependencies =
                std::mem::take(&mut *observed.lock().unwrap_or_else(PoisonError::into_inner));
            result.dependencies.retain(|item| seen.insert(item.clone()));
            Ok(serde_json::to_value(result)?)
        });
    let _ = peer.respond(id, result.map_err(rpc_error)).await;
}

/// Decode capability options into the handler's parameter type.
fn params<P: DeserializeOwned>(options: Options) -> serde_json::Result<P> {
    serde_json::from_value(Value::Object(options))
}

fn schema<T: JsonSchema>() -> Result<Value> {
    Ok(serde_json::to_value(schemars::schema_for!(T))?)
}

fn rpc_error(error: anyhow::Error) -> RpcError {
    RpcError::Remote {
        code: RpcError::SERVER_ERROR,
        message: format!("{error:#}"),
    }
}
