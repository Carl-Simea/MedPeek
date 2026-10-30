#!/usr/bin/env bash
# Start the backend API on port 8000 (installs Python packages on first run).
#   ./run.sh               real Gemma 4 (needs Ollama: bash ~/start-gemma.sh)
#   MOCK_GEMMA=1 ./run.sh  fake answers, no Gemma needed (for frontend work)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
  else
    python3 -m venv .venv
  fi
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install -q --python .venv/bin/python -r requirements.txt
else
  .venv/bin/pip install -q -r requirements.txt
fi

[ -f .env ] || cp .env.example .env
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" ${RELOAD:+--reload}
