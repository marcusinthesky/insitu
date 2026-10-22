//! Concurrency lives here, not in domain code. The host owns inbound dispatch.
use crate::{read_frame, write_frame};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, MutexGuard, PoisonError,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWrite, AsyncWriteExt, BufReader},
    sync::{mpsc, oneshot},
    task::AbortHandle,
};

type Reply = Result<Value, RpcError>;
type Writer = Box<dyn AsyncWrite + Unpin + Send>;
type Pending = BTreeMap<u64, oneshot::Sender<Reply>>;

#[derive(Clone, Debug, thiserror::Error)]
pub enum RpcError {
    #[error("RPC transport: {0}")]
    Transport(String),
    #[error("RPC protocol: {0}")]
    Protocol(String),
    #[error("{message}")]
    Remote { code: i64, message: String },
    #[error("RPC request timed out")]
    Timeout,
    #[error("RPC peer closed")]
    Closed,
}

impl RpcError {
    /// JSON-RPC code for a method the receiver does not implement.
    pub const METHOD_NOT_FOUND: i64 = -32601;
    /// JSON-RPC code for an application error, such as a failed handler.
    pub const SERVER_ERROR: i64 = -32000;
}

/// An inbound call, annotated with the host's connection identifier.
#[derive(Debug)]
pub struct Incoming {
    pub source: usize,
    pub id: Option<Value>,
    pub method: String,
    pub params: Value,
}

struct State {
    writer: tokio::sync::Mutex<Writer>,
    pending: Mutex<Pending>,
    sequence: AtomicU64,
    closed: AtomicBool,
}

impl State {
    /// The requests awaiting a response. The lock is never held across an await.
    fn pending(&self) -> MutexGuard<'_, Pending> {
        self.pending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn fail_pending(&self) {
        self.closed.store(true, Ordering::Release);
        let pending = std::mem::take(&mut *self.pending());
        for sender in pending.into_values() {
            let _ = sender.send(Err(RpcError::Closed));
        }
    }
}

/// Cloneable handle. Responses are routed independently of inbound handlers.
#[derive(Clone)]
pub struct Peer {
    state: Arc<State>,
    reader: Arc<AbortHandle>,
}

struct PendingGuard {
    state: Arc<State>,
    id: u64,
}

impl Drop for PendingGuard {
    fn drop(&mut self) {
        self.state.pending().remove(&self.id);
    }
}

impl Peer {
    /// Attach independent read/write streams and start routing responses.
    pub fn connect<R, W>(
        reader: R,
        writer: W,
        incoming: mpsc::UnboundedSender<Incoming>,
        source: usize,
    ) -> Self
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let state = Arc::new(State {
            writer: tokio::sync::Mutex::new(Box::new(writer)),
            pending: Mutex::new(BTreeMap::new()),
            sequence: AtomicU64::new(0),
            closed: AtomicBool::new(false),
        });
        let task = tokio::spawn(read_loop(reader, Arc::clone(&state), incoming, source));
        Self {
            state,
            reader: Arc::new(task.abort_handle()),
        }
    }

    async fn send(&self, message: Value) -> Result<(), RpcError> {
        if self.state.closed.load(Ordering::Acquire) {
            return Err(RpcError::Closed);
        }
        let payload =
            serde_json::to_vec(&message).map_err(|e| RpcError::Protocol(e.to_string()))?;
        let mut writer = self.state.writer.lock().await;
        write_frame(&mut *writer, &payload)
            .await
            .map_err(|e| RpcError::Transport(e.to_string()))
    }

    /// Send a request. Cancellation and timeout remove the pending request.
    pub async fn request(&self, method: &str, params: Value, timeout: Duration) -> Reply {
        let id = self
            .state
            .sequence
            .fetch_add(1, Ordering::Relaxed)
            .checked_add(1)
            .ok_or_else(|| RpcError::Protocol("request ID exhausted".into()))?;
        let (sender, receiver) = oneshot::channel();
        self.state.pending().insert(id, sender);
        let _guard = PendingGuard {
            state: Arc::clone(&self.state),
            id,
        };
        self.send(json!({"jsonrpc":"2.0", "id":id, "method":method, "params":params}))
            .await?;
        tokio::time::timeout(timeout, receiver)
            .await
            .map_err(|_| RpcError::Timeout)?
            .map_err(|_| RpcError::Closed)?
    }

    /// Reply to a request. Notifications deliberately produce no response.
    pub async fn respond(&self, id: Option<Value>, result: Reply) -> Result<(), RpcError> {
        let Some(id) = id else {
            return Ok(());
        };
        let message = match result {
            Ok(value) => json!({"jsonrpc":"2.0", "id":id, "result":value}),
            Err(error) => {
                let code = match &error {
                    RpcError::Remote { code, .. } => *code,
                    _ => RpcError::SERVER_ERROR,
                };
                json!({"jsonrpc":"2.0", "id":id, "error":{"code":code,"message":error.to_string()}})
            }
        };
        self.send(message).await
    }

    /// Stop routing, fail outstanding requests, and close the write stream.
    pub async fn close(&self) {
        self.reader.abort();
        self.state.fail_pending();
        let _ = self.state.writer.lock().await.shutdown().await;
    }
}

/// Route frames until EOF or a malformed message: requests and notifications
/// go to the host's channel, responses to the request awaiting them.
async fn read_loop(
    reader: impl AsyncRead + Unpin,
    state: Arc<State>,
    incoming: mpsc::UnboundedSender<Incoming>,
    source: usize,
) {
    let mut reader = BufReader::new(reader);
    while let Ok(Some(payload)) = read_frame(&mut reader).await {
        let Ok(message) = serde_json::from_slice::<Value>(&payload) else {
            break;
        };
        if message.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            break;
        }
        if let Some(method) = message.get("method").and_then(Value::as_str) {
            let event = Incoming {
                source,
                id: message.get("id").cloned(),
                method: method.into(),
                params: message.get("params").cloned().unwrap_or(Value::Null),
            };
            if incoming.send(event).is_err() {
                break;
            }
        } else if let Some(id) = message.get("id").and_then(Value::as_u64) {
            let sender = state.pending().remove(&id);
            if let Some(sender) = sender {
                let _ = sender.send(reply(&message));
            }
        }
    }
    state.fail_pending();
}

/// A response's `result`, or its `error` as [`RpcError::Remote`].
fn reply(message: &Value) -> Reply {
    if let Some(error) = message.get("error") {
        return Err(RpcError::Remote {
            code: error
                .get("code")
                .and_then(Value::as_i64)
                .unwrap_or(RpcError::SERVER_ERROR),
            message: error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("remote error")
                .into(),
        });
    }
    message
        .get("result")
        .cloned()
        .ok_or_else(|| RpcError::Protocol("response has neither result nor error".into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn peer_round_trip_and_callback() {
        let (a, b) = tokio::io::duplex(4096);
        let (ar, aw) = tokio::io::split(a);
        let (br, bw) = tokio::io::split(b);
        let (txa, mut rxa) = mpsc::unbounded_channel();
        let (txb, mut rxb) = mpsc::unbounded_channel();
        let host = Peer::connect(ar, aw, txa, 0);
        let ext = Peer::connect(br, bw, txb, 1);
        let task_host = host.clone();
        let request = tokio::spawn(async move {
            task_host
                .request("run", Value::Null, Duration::from_secs(2))
                .await
        });
        let call = rxb.recv().await.unwrap();
        let ext2 = ext.clone();
        let callback = tokio::spawn(async move {
            ext2.request("host.resource.get", Value::Null, Duration::from_secs(2))
                .await
        });
        let event = rxa.recv().await.unwrap();
        host.respond(event.id, Ok(json!(42))).await.unwrap();
        let value = callback.await.unwrap().unwrap();
        ext.respond(call.id, Ok(value)).await.unwrap();
        assert_eq!(request.await.unwrap().unwrap(), json!(42));
        host.close().await;
        ext.close().await;
    }
}
