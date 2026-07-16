#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export CX2CC_HOST="${CX2CC_HOST:-127.0.0.1}"
export CX2CC_PORT="${CX2CC_PORT:-8901}"
export PATH="/usr/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

cd "$ROOT"
if [ -x "$ROOT/cx2cc" ]; then
    exec "$ROOT/cx2cc" serve
fi
if [ -x "$ROOT/.venv/bin/python" ]; then
    exec "$ROOT/.venv/bin/python" "$ROOT/start-cx2cc.py" serve
fi
exec /usr/bin/env python3 "$ROOT/start-cx2cc.py" serve
