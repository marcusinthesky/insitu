---
title: Fixture
description: "Insitu projection test fixture"
insitu:
  materializations:
    counts:
      kind: sql
      model: counts
      target:
        kind: frontmatter
        pointer: /generated/counts
---

## Fixture

<!-- insitu:begin sql
id = "tasks"
model = "open_tasks"
template = "tasks.md.j2"
-->
stale
<!-- insitu:end -->

<!-- insitu:begin tree
id = "tree"
depth = 1
exclude = [".insitu/**"]
-->
stale
<!-- insitu:end -->

<!-- insitu:begin include
id = "snippet"
path = "snippets/example.py"
language = "python"
-->
stale
<!-- insitu:end -->
