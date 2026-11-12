#!/bin/sh
# Start the breadcrumbs MCP server (stdio) with a Python of 3.11 or newer. PYTHONPATH comes from .mcp.json:
# lib/ holds breadcrumbs-common (put there by breadcrumbs/tools/bundle.py), this folder holds breadcrumbs_claude.
# BREADCRUMBS_PY names the Python to use.
for py in "$BREADCRUMBS_PY" python3.13 python3.12 python3.11 python3; do
  [ -n "$py" ] || continue
  if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$py" -m breadcrumbs core mcp
  fi
done
echo "breadcrumbs: needs Python 3.11 or newer (set BREADCRUMBS_PY)" >&2
exit 1
