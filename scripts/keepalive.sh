#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export KEEPALIVE_TOKEN="${1:-${KEEPALIVE_TOKEN:-}}"
export BASE_URL="${2:-${BASE_URL:-}}"
export MODEL="${3:-${MODEL:-}}"

if [ -z "${PYTHON_BIN:-}" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi

if [ -z "${PYTHON_BIN:-}" ]; then
    echo "ERROR: Python 3.11+ not found. Set PYTHON_BIN to your Python executable." >&2
    exit 2
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/codex_probe.py"
