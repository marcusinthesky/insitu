---
description: Reactive, typed materializations for Markdown and YAML front matter.
---

# Insitu

Insitu keeps generated Markdown and YAML front matter synchronized with source
files in your project. It can insert local files, render directory trees, and
produce views from SQL and Jinja templates.

## Quickstart

Use Python 3.13 in a uv project, then install the current Git version:

```bash
uv add git+ssh://git@github.com/marcusinthesky/insitu.git
uv run insitu init
```

Create `snippet.txt` and `notes.md` in the project root:

```text
Hello from Insitu!
```

```markdown
# Notes

<!-- insitu:begin include
id = "example"
path = "snippet.txt"
language = "text"
-->

<!-- insitu:end -->
```

Run `uv run insitu sync` to fill the managed region. Then run
`uv run insitu check`: it exits successfully when generated content is current.
The same files are in [the runnable example](examples/quickstart/notes.md).
For a pip environment, install with
`python -m pip install 'git+ssh://git@github.com/marcusinthesky/insitu.git'`
and invoke `insitu` directly. Pin a Git commit or tag when you need a repeatable
installation. GitHub access to this private repository is required.

Continue with the [getting started guide](docs/getting-started.md) and
[API reference](docs/reference.md). Use `uv run insitu watch` to keep generated
content current during editing.

## Development

`direnv allow` loads the pinned devenv environment and synchronizes uv
dependencies. Run `just check` for the same prek gates as CI, `just test` for
tests, `just docs-check` for documentation, or `just watch` to rerun tests on
source changes.

Extensions are trusted processes: they inherit the invoking user's environment
and filesystem access, despite Insitu's host-mediated resource permissions.
