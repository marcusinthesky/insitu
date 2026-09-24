"""Strict Jinja rendering for typed resource values."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jinja2 import (
    ChoiceLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
)

from insitu.types import JsonValue, Location

_BUILTINS = {
    "@insitu/table": """{% if rows %}| {% for key in rows[0] %}{{ key }} |{% endfor %}
|{% for key in rows[0] %} --- |{% endfor %}
{% for row in rows -%}| {% for value in row.values() %}{{ value }} |{% endfor %}
{% endfor %}{% else %}_No rows._{% endif %}""",
    "@insitu/list": """{% for row in rows -%}- {{ row }}
{% else %}_No rows._{% endfor %}""",
    "@insitu/json": "```json\n{{ value | tojson(indent=2) }}\n```",
}


class JinjaRenderer:
    """Render user and built-in templates with a small stable context."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.environment = Environment(
            loader=ChoiceLoader([DictLoader(_BUILTINS), FileSystemLoader(root)]),
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.environment.filters["relative_to"] = self._relative_to
        self.environment.filters["json"] = lambda value: json.dumps(
            value,
            sort_keys=True,
        )

    def render(
        self,
        template: str,
        *,
        rows: list[dict[str, object]],
        params: dict[str, JsonValue],
        location: Location,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Render one template using query rows and location context."""
        context: dict[str, Any] = {
            "rows": rows,
            "params": params,
            "this": location.model_dump(),
        }
        context.update(extra or {})
        return self.environment.get_template(template).render(context).rstrip()

    def _relative_to(self, value: str, directory: str | None = None) -> str:
        base = self.root / (directory or "")
        return Path(os.path.relpath(self.root / value, base)).as_posix()
