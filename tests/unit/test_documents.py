"""Managed document codec tests."""

import pytest

from insitu.documents import apply_frontmatter_patches, parse_regions, replace_regions
from insitu.types import FrontMatterSetPatch

pytestmark = pytest.mark.unit


def test_region_and_frontmatter_patches_compose() -> None:
    text = """---
title: Demo
---
<!-- insitu:begin tree
id = "x"
-->
old
<!-- insitu:end -->
"""
    region = parse_regions(text)[0]
    assert (region.id, region.kind) == ("x", "tree")
    updated = replace_regions(text, {"x": "new"})
    updated = apply_frontmatter_patches(
        updated,
        [FrontMatterSetPatch(path="README.md", pointer="/generated/count", value=2)],
    )
    assert "\n\nnew\n\n<!-- insitu:end -->" in updated
    assert "generated:\n  count: 2" in updated


def test_regions_inside_fences_are_examples() -> None:
    text = """```markdown
<!-- insitu:begin sql
id = \"example\"
-->
<!-- insitu:end -->
```

<!-- insitu:begin tree
id = \"real\"
-->
old
<!-- insitu:end -->
"""
    regions = parse_regions(text)
    assert [region.id for region in regions] == ["real"]
