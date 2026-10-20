//! Insitu protocol v1: typed boundaries and a bidirectional stdio peer.
//!
//! The transport borrows LSP framing, not the LSP method set. Application
//! diagnostics go to stderr; stdout is exclusively framed JSON-RPC.

mod framing;
mod models;
mod peer;

pub use framing::{MAX_MESSAGE_BYTES, read_frame, write_frame};
pub use models::*;
pub use peer::{Incoming, Peer, RpcError};
