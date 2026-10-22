---
description: The engine — scan, SQLite index, plan, render, then a checked, staged commit of every changed file.
start_here: src/project.rs
---

# insitu-core

`Project::run` is one reconciliation pass. Scanning and indexing live in
`index.rs`, region parsing and front-matter edits in `document.rs`, SQL models and
templates in `builtins.rs`, extension processes in `extensions.rs`, and the staged
write in `patch.rs`. Domain calculations are synchronous; only process I/O is async.
