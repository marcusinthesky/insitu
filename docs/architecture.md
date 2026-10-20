# Architecture and reading guide

## One owner, concrete data

The CLI owns a `Project`. Project owns Config, Index, Builtins, Registry and
Process handles. Pure computations remain synchronous. There is no shared mutable
engine behind an async mutex, no general actor framework and no generic repository
backend. Each invocation carries owned options, location and dependency trace.

`Project::run` scans/indexes, plans concrete jobs, validates capabilities, renders,
prepares a ChangeSet and commits only in Sync mode. A report is plain data. Check
uses the same calculations but reports stale files and never commits the ChangeSet.
Most operational errors use contextual `anyhow::Result`; RPC has a concrete error
enum. A small BTreeMap-based graph makes ordering deterministic.

## Files and ownership

`Document` owns the source string and byte ranges. Regions are recognized outside
fenced code; output is replaced only between the opening and closing markers.
`patch.rs` stages complete replacement files on the destination filesystem, checks
original bytes before writes and attempts rollback after a replacement failure.
YAML syntax trees exist only inside synchronous editing functions; they do not
cross Tokio task boundaries.

The engine separates authored source from generated output. SQL sees a projection
without generated Markdown bodies and without front-matter `insitu`/`generated`.
An ownership graph relates files, output targets and materializations; a self-include
creates a dependency cycle instead of repeatedly expanding the document.

## Extension concurrency

Each process has a `Peer`. Its reader task routes numeric-ID responses independently
from inbound method calls. While Project awaits an extension result, it services
callbacks from an inbound channel. A callback can resolve a nested extension
resource without blocking the peer's response reader. Recursive capability/resource
methods use a boxed future only at this boundary.

A capability registry holds a native-handler enum or a process index and name.
Transforms run base -> topologically ordered decorators -> contributors. Replacement
requires explicit project approval. Validators produce structured diagnostics.
Subprocesses inherit user privileges; permissions restrict host-mediated requests
but are not an operating-system sandbox.

## Extension authoring

Python keeps the decorator API, Pydantic TypeAdapter validation, sync/async handler
support and Context.resource. Rust uses registration methods with generic parameter
and result types; Serde and Schemars handle the boundary. Domain code does not carry
those generic parameters. Rustdoc on `insitu-sdk` includes a small example.

## Intentional first-pass simplification

The candidate scans and renders everything on each reconciliation. This simplifies
ownership and avoids pretending an untested incremental cache is correct. It is,
however, a compatibility gap for invocation counts and side effects, not merely a
performance claim. See the release blockers before calling the rewrite drop-in.
