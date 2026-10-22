---
description: Version 1 wire format for extensions — validated models and a bidirectional JSON-RPC peer over framed stdio.
start_here: src/peer.rs
---

# insitu-protocol

The transport borrows LSP framing, not the LSP method set: stdout carries only
framed JSON-RPC and diagnostics go to stderr. Both the host and the Rust SDK use
this crate, and the Python SDK implements the same messages; see the
[protocol guide](../../docs/protocol.md).
