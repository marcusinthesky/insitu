# Concepts

A managed region is a Markdown comment pair. Its opening comment contains TOML
options. `kind` chooses a transform such as `include`, and `id` identifies the
region that Insitu owns. The text between the comments is generated output.

`insitu sync` computes and writes current output; `insitu check` computes the
same result without writing. Generated bodies are excluded from Insitu's source
projection so they do not become their own inputs. Other text in the document
remains authored content.

Projects can also declare materializations in YAML front matter, use SQL models
and Jinja templates, or install process extensions. Begin with the
[file inclusion guide](getting-started.md) before using those features.
