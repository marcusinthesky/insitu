---
description: Typed registration for Rust extensions — resources, transforms, renderers and validators as async handlers.
start_here: src/lib.rs
---

# insitu-sdk

An extension depends on this crate alone; it re-exports the protocol types that
handlers use. Serde and Schemars handle the boundary, so handlers take and return
ordinary Rust types. The crate documentation includes a complete extension.
