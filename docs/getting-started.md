# Getting started

Insitu requires Python 3.13. In an existing uv project, install it from Git:

```bash
uv add git+ssh://git@github.com/marcusinthesky/insitu.git
uv run insitu init
```

The `init` command creates `insitu.toml` and starter SQL and Jinja definitions.
It also adds `.insitu/` to `.gitignore`; that directory contains local index state.

Create a `snippet.txt` file beside `notes.md`:

```text
Hello from Insitu!
```

Put a managed region in `notes.md`:

```markdown
# Notes

<!-- insitu:begin include
id = "example"
path = "snippet.txt"
language = "text"
-->

<!-- insitu:end -->
```

Run the commands from the project root:

```bash
uv run insitu sync
uv run insitu check
```

`sync` writes a fenced text snippet between the comments. `check` is read only:
it exits with code 1 if generated content is stale, or code 2 for an error. Run
`uv run insitu watch` while editing to update managed content automatically.

The same source files are available in
[the example project](https://github.com/marcusinthesky/insitu/tree/main/examples/quickstart).
For a pip environment, use
`python -m pip install 'git+ssh://git@github.com/marcusinthesky/insitu.git'`
and invoke `insitu` directly. GitHub access to the private repository is
required; pin a Git commit or tag for repeatable installs.
