#!/bin/bash
# Start the Litestar API under debugpy so VS Code can attach remotely.
# debugpy listens on 127.0.0.1 only — reach it from your laptop via SSH tunnel:
#   ssh -L 5678:localhost:5678 ramboq
#
# Then in VS Code: Run > Start Debugging > "Remote: Attach to Dev API"
# Attach/detach at any time — server runs normally when no debugger is connected.
# Note: --no-reload is required (uvicorn reload forks the process, breaking debugpy).

set -e
cd "$(dirname "$0")/.."
source venv/bin/activate

PORT=${API_PORT:-8001}
DBG_PORT=${DBG_PORT:-5678}

exec python -Xfrozen_modules=off -m debugpy --listen "127.0.0.1:${DBG_PORT}" \
  -m uvicorn backend.api.app:app \
  --host 0.0.0.0 --port "${PORT}" --workers 1 --log-level info
