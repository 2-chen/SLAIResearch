#!/usr/bin/env python3
"""
Prompt template renderer for SLAIResearch.

Reads a template file with ${VAR} placeholders and renders it with values
from environment variables and command-line arguments.

Usage:
  python prompt_render.py <template.md> [VAR=value ...] > /tmp/output.txt

Template variables are written as ${VAR_NAME} (same as bash).
Values are read from:
  1. Command-line VAR=value arguments (highest priority)
  2. Environment variables
  3. If neither found, the placeholder is left as-is (preserved for debugging)
"""

import os
import re
import sys
from pathlib import Path


def render_template(template_path: str, extra_vars: dict[str, str]) -> str:
    """Read a template file and substitute ${VAR} placeholders."""
    template = Path(template_path).read_text(encoding="utf-8")

    # Build variable map: env vars + extra vars (extra overrides env)
    var_map: dict[str, str] = {}
    var_map.update(os.environ)
    var_map.update(extra_vars)

    # Substitute ${VAR} patterns
    def _replace(m: re.Match) -> str:
        name = m.group(1)
        return var_map.get(name, m.group(0))

    return re.sub(r"\$\{(\w+)\}", _replace, template)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <template.md> [VAR=value ...]", file=sys.stderr)
        sys.exit(1)

    template_path = sys.argv[1]
    extra_vars: dict[str, str] = {}
    for arg in sys.argv[2:]:
        if "=" in arg:
            key, _, value = arg.partition("=")
            extra_vars[key] = value

    if not Path(template_path).exists():
        print(f"ERROR: Template not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    rendered = render_template(template_path, extra_vars)
    sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
