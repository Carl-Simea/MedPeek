#!/usr/bin/env bash
# Runs the MedPeek web app. Backend URL defaults to the local backend (same machine / Codespace).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  (command -v uv >/dev/null && uv venv -q -p 3.12 .venv) || python3 -m venv .venv
fi
if command -v uv >/dev/null; then uv pip install -q --python .venv/bin/python -r requirements.txt; else .venv/bin/python -m pip install -q -r requirements.txt; fi
export XRAY_BACKEND_URL="${XRAY_BACKEND_URL:-http://127.0.0.1:8000}"
export HOST="${HOST:-0.0.0.0}" PORT="${PORT:-5000}"
echo "MedPeek web app on http://localhost:$PORT (backend: $XRAY_BACKEND_URL)"
exec .venv/bin/python app.py
