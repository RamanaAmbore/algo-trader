#!/bin/bash
# Webhook dispatch script — deployed to /etc/webhook/dispatch.sh
# Routes incoming push events to the correct environment's deploy script.
# Usage: dispatch.sh <REF>  (called by webhook with ref as first arg)

REF="${1:-refs/heads/main}"
BRANCH="${REF#refs/heads/}"

if [ "$BRANCH" = "main" ]; then
    exec /opt/ramboq/webhook/deploy.sh prod "$REF"
elif [ "$BRANCH" = "dev" ]; then
    exec /opt/ramboq_dev/webhook/deploy.sh dev "$REF"
else
    # workshop (local-only, no deploy target) and any other branch: no-op.
    # Previously this fell through to the dev deploy for ANY non-main push,
    # so a workshop push queued a redundant dev-deploy behind the real one,
    # delaying a concurrent main push on the shared /tmp/ramboq_deploy.lock.
    echo "[$(date '+%F %T')] dispatch: branch '$BRANCH' has no deploy target, skipping"
    exit 0
fi
