# Process protocol v1

Transport is bidirectional JSON-RPC 2.0 on standard streams. Each UTF-8 JSON payload
is preceded by `Content-Length: <byte length>\r\n\r\n`. Standard output is reserved
for protocol frames; logs go to standard error. Both peers can issue requests while
awaiting responses. Requests carry IDs; notifications do not produce replies.

The candidate enforces a 16 MiB payload cap and an additional 8 KiB header cap.
Duplicate Content-Length headers and incomplete frames are rejected. Malformed
JSON terminates a connection. The host's default request timeout is 30 seconds;
shutdown requests use a shorter timeout. Request cancellation removes local pending
state; it does not claim support for an unimplemented remote cancellation method.

## Lifecycle and methods

The host reads `insitu-extension.toml`, launches the declared command, then calls
`insitu.initialize` with protocol=1, absolute project root, and extension name.
The extension returns name, version, protocol and capability schemas. The host checks
identity and any schemas explicitly declared in the manifest.

Host methods: `insitu.initialize`, `insitu.transform`, `insitu.resource`,
`insitu.render`, `insitu.validate`, `insitu.shutdown`.
Extension callback: `host.resource.get`.

An invocation contains name, options, location, value (null by default), and trace.
A result contains content, value, diagnostics and dependencies. Unknown boundary
fields are rejected. Diagnostic severities are error, warning and info. Capability
kinds are resource, transform, renderer and validator.

A resource read returns a JSON value and flattened transitive dependencies. Reads
support `fs:file:`, `fs:glob:`, `sql:model:`, `resource:` and `git://` identifiers.
Corresponding permission prefixes are `fs:read:`, `sql:read:`, `resource:read:` and
`git:read:`. A permission is not filesystem isolation: a trusted process can still
perform its own I/O outside the host API.

Inspect `crates/insitu-protocol/src/models.rs` and `python/insitu/types.py` for the
boundary types. Python tests execute the Python side of this protocol.
