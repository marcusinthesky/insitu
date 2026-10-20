//! Typed, asynchronous authoring of v1 process extensions.
//!
//! ```no_run
//! use insitu_sdk::{Extension, Context};
//! use insitu_protocol::Options;
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
    CapabilityKind as Kind, Diagnostic, InitializeRequest, InitializeResult, InvocationRequest,
    InvocationResult, Location, Peer, ResourceRead, ResourceRequest, RpcError,
};
use schemars::JsonSchema;
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    future::Future,
    path::PathBuf,
    pin::Pin,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::mpsc;

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
            .unwrap_or_else(|p| p.into_inner())
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
    registrations: BTreeMap<(Kind, String), Arc<Registration>>,
}
impl Extension {
    pub fn new(name: impl Into<String>, version: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            version: version.into(),
            registrations: BTreeMap::new(),
        }
    }

    pub fn transform<P, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        P: DeserializeOwned + JsonSchema + 'static,
        F: Fn(P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<String>> + Send + 'static,
    {
        self.insert(
            Kind::Transform,
            name,
            json!({"params":schema::<P>()?,"result":schema::<String>()?}),
            move |request, context| {
                let params = serde_json::from_value::<P>(Value::Object(request.options));
                match params {
                    Ok(params) => {
                        let future = handler(params, context);
                        Box::pin(async move {
                            Ok(InvocationResult {
                                content: Some(future.await?),
                                ..Default::default()
                            })
                        })
                    }
                    Err(error) => Box::pin(async move { Err(error.into()) }),
                }
            },
        )
    }
    pub fn resource<P, R, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        P: DeserializeOwned + JsonSchema + 'static,
        R: Serialize + JsonSchema + 'static,
        F: Fn(P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<R>> + Send + 'static,
    {
        self.insert(
            Kind::Resource,
            name,
            json!({"params":schema::<P>()?,"result":schema::<R>()?}),
            move |request, context| {
                let params = serde_json::from_value::<P>(Value::Object(request.options));
                match params {
                    Ok(params) => {
                        let future = handler(params, context);
                        Box::pin(async move {
                            Ok(InvocationResult {
                                value: serde_json::to_value(future.await?)?,
                                ..Default::default()
                            })
                        })
                    }
                    Err(error) => Box::pin(async move { Err(error.into()) }),
                }
            },
        )
    }
    pub fn renderer<V, P, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        V: DeserializeOwned + JsonSchema + 'static,
        P: DeserializeOwned + JsonSchema + 'static,
        F: Fn(V, P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<String>> + Send + 'static,
    {
        self.insert(
            Kind::Renderer,
            name,
            json!({"value":schema::<V>()?,"params":schema::<P>()?,"result":schema::<String>()?}),
            move |request, context| {
                let decoded = (|| {
                    Ok::<_, serde_json::Error>((
                        serde_json::from_value::<V>(request.value)?,
                        serde_json::from_value::<P>(Value::Object(request.options))?,
                    ))
                })();
                match decoded {
                    Ok((value, params)) => {
                        let future = handler(value, params, context);
                        Box::pin(async move {
                            Ok(InvocationResult {
                                content: Some(future.await?),
                                ..Default::default()
                            })
                        })
                    }
                    Err(error) => Box::pin(async move { Err(error.into()) }),
                }
            },
        )
    }
    pub fn validator<P, F, Fut>(&mut self, name: &str, handler: F) -> Result<&mut Self>
    where
        P: DeserializeOwned + JsonSchema + 'static,
        F: Fn(P, Context) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = Result<Vec<Diagnostic>>> + Send + 'static,
    {
        self.insert(
            Kind::Validator,
            name,
            json!({"params":schema::<P>()?,"result":schema::<Vec<Diagnostic>>()?}),
            move |request, context| {
                let params = serde_json::from_value::<P>(Value::Object(request.options));
                match params {
                    Ok(params) => {
                        let future = handler(params, context);
                        Box::pin(async move {
                            Ok(InvocationResult {
                                diagnostics: future.await?,
                                ..Default::default()
                            })
                        })
                    }
                    Err(error) => Box::pin(async move { Err(error.into()) }),
                }
            },
        )
    }
    fn insert(
        &mut self,
        kind: Kind,
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
        let mut tasks = tokio::task::JoinSet::new();
        while let Some(call) = incoming.recv().await {
            match call.method.as_str() {
                "insitu.initialize" => {
                    let result = (|| -> Result<Value> {
                        let request: InitializeRequest = serde_json::from_value(call.params)?;
                        root = Some(PathBuf::from(request.root).canonicalize()?);
                        let mut capabilities = BTreeMap::new();
                        for kind in [
                            Kind::Resource,
                            Kind::Transform,
                            Kind::Renderer,
                            Kind::Validator,
                        ] {
                            let entries: serde_json::Map<String, Value> = self
                                .registrations
                                .iter()
                                .filter(|((k, _), _)| *k == kind)
                                .map(|((_, name), registration)| {
                                    (name.clone(), registration.schema.clone())
                                })
                                .collect();
                            capabilities.insert(kind.as_str().into(), Value::Object(entries));
                        }
                        Ok(serde_json::to_value(InitializeResult {
                            name: self.name.clone(),
                            version: self.version.clone(),
                            protocol: 1,
                            capabilities,
                        })?)
                    })();
                    peer.respond(call.id, result.map_err(rpc_error)).await?;
                }
                "insitu.shutdown" => {
                    peer.respond(call.id, Ok(Value::Null)).await?;
                    break;
                }
                method => {
                    let kind = match method {
                        "insitu.transform" => Some(Kind::Transform),
                        "insitu.resource" => Some(Kind::Resource),
                        "insitu.render" => Some(Kind::Renderer),
                        "insitu.validate" => Some(Kind::Validator),
                        _ => None,
                    };
                    let decoded = (|| -> Result<_> {
                        let root = root.clone().context("extension has not been initialized")?;
                        let kind = kind.context("unknown method")?;
                        let request: InvocationRequest = serde_json::from_value(call.params)?;
                        let registration = Arc::clone(
                            self.registrations
                                .get(&(kind, request.name.clone()))
                                .context("unknown capability")?,
                        );
                        Ok((root, request, registration))
                    })();
                    match decoded {
                        Err(error) => peer.respond(call.id, Err(rpc_error(error))).await?,
                        Ok((root, request, registration)) => {
                            let peer = peer.clone();
                            tasks.spawn(async move {
                                let context = Context {
                                    root,
                                    location: request.location.clone(),
                                    peer: peer.clone(),
                                    trace: request.trace.clone(),
                                    dependencies: Arc::new(Mutex::new(vec![])),
                                };
                                let observed = Arc::clone(&context.dependencies);
                                let result = (registration.handler)(request, context)
                                    .await
                                    .and_then(|mut result| {
                                        result.dependencies = observed
                                            .lock()
                                            .unwrap_or_else(|p| p.into_inner())
                                            .clone();
                                        let mut seen = std::collections::BTreeSet::new();
                                        result
                                            .dependencies
                                            .retain(|item| seen.insert(item.clone()));
                                        Ok(serde_json::to_value(result)?)
                                    });
                                let _ = peer.respond(call.id, result.map_err(rpc_error)).await;
                            });
                        }
                    }
                }
            }
            while tasks.try_join_next().is_some() {}
        }
        tasks.abort_all();
        while tasks.join_next().await.is_some() {}
        peer.close().await;
        Ok(())
    }
}
fn schema<T: JsonSchema>() -> Result<Value> {
    Ok(serde_json::to_value(schemars::schema_for!(T))?)
}
fn rpc_error(error: anyhow::Error) -> RpcError {
    RpcError::Remote {
        code: -32000,
        message: format!("{error:#}"),
    }
}
