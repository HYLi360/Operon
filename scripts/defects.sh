#!/usr/bin/env bash

# Append to and query the Operon defect registry (defects.yml + defects/*.yml).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname -- "$SCRIPT_DIR")"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

exec "$PYTHON" "$SCRIPT_DIR/defects.py" "$@"
