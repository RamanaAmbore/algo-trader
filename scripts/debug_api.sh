#!/bin/bash
# Start the Litestar API under debugpy so VS Code can attach remotely.
#
# On dev server — stop the systemd service first, then run this script:
#   sudo systemctl stop ramboq_dev_api
#   bash scripts/debug_api.sh
#
# On your laptop — open the SSH tunnel in a terminal:
#   ssh -L 5678:localhost:5678 ramboq
#
# Then in VS Code: Run > Start Debugging > "Remote: Attach to Dev API"
#
# Restart the service when done:
#   sudo systemctl start ramboq_dev_api

set -e
cd "$(dirname "$0")/.."
source venv/bin/activate

PORT=${API_PORT:-8001}
DBG_PORT=${DBG_PORT:-5678}

echo "debugpy listening on :${DBG_PORT}  |  API on :${PORT}  |  reload=off"
echo "SSH tunnel: ssh -L ${DBG_PORT}:localhost:${DBG_PORT} ramboq"
echo ""

exec python -m debugpy --listen "0.0.0.0:${DBG_PORT}" \
  -m uvicorn backend.api.app:app \
  --host 0.0.0.0 --port "${PORT}" --no-reload
