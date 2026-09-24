# Architecture

```text
watchfiles → source index → resource DAG → transforms → typed patches → atomic write
```

Core owns project discovery, subscriptions, cycle checks, extension lifecycle,
patch conflicts, and `sync`/`check`/`watch` equivalence. Built-ins own SQL,
Jinja, directory trees, and source inclusion.

Markdown output and `/generated/*` front matter are removed from the default
source projection. This prevents generated content from becoming an implicit
input to its own materialization.
