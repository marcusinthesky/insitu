---
description: The command line, its reports and the filesystem watch loop; it computes nothing itself.
start_here: src/main.rs
---

# insitu

The `insitu` binary parses arguments, calls `insitu-core` once per command, and
prints the report as text or `--json`. `watch` debounces native filesystem events
and runs another pass; it keeps no state of its own between passes.
