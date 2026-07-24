#!/bin/bash
# Unified deploy script — handles prod and dev environments.
# Called by /etc/webhook/dispatch.sh with ENV and REF arguments.
# Usage: deploy.sh <ENV> <REF>
#   ENV : prod | dev
#   REF : refs/heads/<branch>  (e.g. refs/heads/main)
#
# Services run uvicorn (Litestar API) + SvelteKit SPA as static files.
# The SvelteKit build (frontend/build/) is served as static files by Litestar.

# D7 — Lock file serialises every deploy on this host. Previously the
# lock was per-environment (`ramboq_deploy_${1:-prod}.lock`) so a push
# to both prod and dev (the standard workflow) ran TWO `npm run build`
# processes simultaneously. Each vite build pegs a CPU at 100 %; the
# 2-vCPU box hit load avg ~1.8 and every page request stalled for the
# 30-60s build window. Operator: "overall response time of website
# degraded. each page is taking more time to load."
#
# Single host-wide lock + WAIT (not non-blocking). The second deploy
# queues for up to 15 min; in practice ~60 s. CPU stays usable for
# the running API process during the first build.
#
# Must come before `set -e`: flock returns non-zero on timeout, which
# we surface explicitly via the message below.
LOCK="/tmp/ramboq_deploy.lock"
exec 200>"$LOCK"
flock -w 900 200 || { echo "[$(date '+%F %T')] Lock wait timeout — aborting"; exit 0; }

set -e

TS=$(date '+%Y-%m-%d %H:%M:%S')
export HOME=/var/www

ENV="${1:-prod}"
REF="${2:-refs/heads/main}"
BRANCH="${REF#refs/heads/}"

case "$ENV" in
  prod) APP_ROOT="/opt/ramboq";     API_SERVICE="ramboq_api.service"     ;;
  dev)  APP_ROOT="/opt/ramboq_dev"; API_SERVICE="ramboq_dev_api.service" ;;
  *) echo "[$TS] ERROR: unknown ENV '$ENV'"; exit 1 ;;
esac

LOG="$APP_ROOT/.log/hook_debug.log"

# D9 — Trap: on any non-zero exit, fire a failure notification before the
# process dies. on_exit receives $? from the trap. Best-effort — don't let
# the notification itself cause a secondary failure.
on_exit() {
    EXIT_CODE=$1
    if [ "$EXIT_CODE" -ne 0 ]; then
        echo "[$TS] DEPLOY FAILED with exit code $EXIT_CODE"
        python "$APP_ROOT/webhook/notify_deploy.py" \
            --status fail \
            --branch "$BRANCH" \
            --commit "$(git -C "$APP_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
            --reason "exit code $EXIT_CODE during deploy" 2>&1 || true
    fi
}
trap 'on_exit $?' EXIT

{
  # Abort on any error so a failed git pull doesn't get masked by a successful
  # restart further down. The previous version swallowed git failures and
  # reported "Deployment complete" even when the working tree was stale.

  echo "[$TS] Deploy triggered: $ENV (branch: $BRANCH)"
  echo "Running as: $(whoami)"

  cd "$APP_ROOT" || { echo "[$TS] ERROR: cannot cd to $APP_ROOT"; exit 1; }

  # D3 — Self-healing chown BEFORE git ops so subsequent git/npm operations
  # don't choke on root-owned files left by aborted deploys or manual SSH work.
  # Idempotent — harmless on a clean tree.
  sudo chown -R www-data:www-data \
      "$APP_ROOT/.git" "$APP_ROOT/.log" "$APP_ROOT/backend/config" \
      "$APP_ROOT/frontend/.svelte-kit" "$APP_ROOT/frontend/build" \
      "$APP_ROOT/frontend/node_modules" 2>/dev/null || true

  git --git-dir="$APP_ROOT/.git" --work-tree="$APP_ROOT" config --add safe.directory "$APP_ROOT"

  # One-time migration: rename old config file names to new names
  [ -f "backend/config/config.yaml" ] && [ ! -f "backend/config/backend_config.yaml" ] && \
    mv "backend/config/config.yaml" "backend/config/backend_config.yaml" && \
    echo "[$TS] Migrated config.yaml → backend_config.yaml"
  [ -f "backend/config/ramboq_config.yaml" ] && [ ! -f "backend/config/frontend_config.yaml" ] && \
    mv "backend/config/ramboq_config.yaml" "backend/config/frontend_config.yaml" && \
    echo "[$TS] Migrated ramboq_config.yaml → frontend_config.yaml"
  [ -f "backend/config/ramboq_constants.yaml" ] && [ ! -f "backend/config/constants.yaml" ] && \
    mv "backend/config/ramboq_constants.yaml" "backend/config/constants.yaml" && \
    echo "[$TS] Migrated ramboq_constants.yaml → constants.yaml"

  # Save server-specific backend_config.yaml flags before git operations overwrite it
  CONFIG_BAK="/tmp/ramboq_config_$$.yaml"
  [ -f "backend/config/backend_config.yaml" ] && cp "backend/config/backend_config.yaml" "$CONFIG_BAK"

  # --- Git update ---
  # Hard-reset against origin instead of `git pull` so any local working-tree
  # drift (npm-regenerated package-lock.json, sed-edited backend_config.yaml,
  # editor swap files) doesn't block the merge. The only "local changes" the
  # working tree carries are deploy-induced; treating them as authoritative
  # over origin caused 3 silent deploy-failures previously.
  PREV_HEAD=$(git rev-parse HEAD)
  git fetch origin "$BRANCH"
  git checkout -B "$BRANCH" "origin/$BRANCH" 2>/dev/null || git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
  CHANGED=$(git diff --name-only "$PREV_HEAD" HEAD)

  # D4 — If this push includes a deploy.sh update, re-exec into the new version.
  # `exec` replaces the current process so the original instance's
  # "Deployment complete" log + notify_deploy.py never fires — only ONE
  # Telegram/email alert per logical deploy.
  if echo "$CHANGED" | grep -q "^webhook/deploy.sh$"; then
      echo "[$TS] deploy.sh changed in this push — re-executing with new version (single notification)"
      exec bash "$APP_ROOT/webhook/deploy.sh" "$ENV" "$REF"
  fi

  # --- Restore / merge server-specific config flags ---
  # Preserves top-level scalars AND the cap_in_dev dict from the server's prior
  # config. Only keys listed in PRESERVE_SCALARS are carried over — new keys
  # introduced in the repo config appear unchanged.
  if [ -f "$CONFIG_BAK" ]; then
    python3 - "$CONFIG_BAK" "backend/config/backend_config.yaml" <<'PYEOF'
import sys, yaml
bak_path, new_path = sys.argv[1], sys.argv[2]
# Operator-tunable scalars used to be preserved here (enforce_password_standard,
# genai_thinking_budget, alert_*, etc.). They've all moved to the `settings` DB
# table, which the seeder preserves the operator's `value` column across deploys
# automatically. PRESERVE_SCALARS is now empty — left in place so adding a new
# YAML-only knob is one entry and not a re-discovery exercise.
PRESERVE_SCALARS = []
with open(bak_path) as f: bak = yaml.safe_load(f) or {}
with open(new_path) as f: new = yaml.safe_load(f) or {}
for k in PRESERVE_SCALARS:
    if k in bak:
        new[k] = bak[k]
# cap_in_dev / cap_in_prod: carry the whole dict from the server's backup
# (so each capability keeps its prior setting). Missing keys from the repo
# default fill in. Same pattern for both sections so operator tweaks on
# either environment survive a deploy.
for sect in ("cap_in_dev", "cap_in_prod"):
    bak_caps = bak.get(sect) or {}
    new_caps = new.get(sect) or {}
    if isinstance(bak_caps, dict) and isinstance(new_caps, dict):
        merged = dict(new_caps)
        merged.update(bak_caps)
        # One-shot rename cleanup: the old key was `sim_mode`, the new one
        # is `simulator`. When both are present, the new value wins; the
        # old key is discarded so it doesn't linger as dead weight.
        if "sim_mode" in merged:
            if "simulator" not in merged:
                merged["simulator"] = merged["sim_mode"]
            merged.pop("sim_mode", None)
        new[sect] = merged
# macros block: same pattern as cap_in_dev — operator's hand-edited values
# (latest RBI / CPI / IIP figures) survive a deploy. Repo defaults fill in
# any missing keys so adding a new macro never requires a server edit.
bak_macros = bak.get("macros") or {}
new_macros = new.get("macros") or {}
if isinstance(bak_macros, dict) and isinstance(new_macros, dict):
    merged = dict(new_macros)
    merged.update(bak_macros)
    new["macros"] = merged
with open(new_path, "w") as f:
    yaml.safe_dump(new, f, default_flow_style=False, sort_keys=False)
PYEOF
    rm -f "$CONFIG_BAK"
  fi

  # Write current branch into config
  if grep -q "^deploy_branch:" "backend/config/backend_config.yaml" 2>/dev/null; then
    sed -i "s/^deploy_branch:.*/deploy_branch: ${BRANCH}/" "backend/config/backend_config.yaml"
  else
    echo "deploy_branch: ${BRANCH}" >> "backend/config/backend_config.yaml"
  fi

  echo "[$TS] Changed files:"
  echo "$CHANGED"

  # ─── Selective restart: protect broker connections from FE-only pushes ───
  # Most pushes are CSS / Svelte / static-asset tweaks that don't touch the
  # Python API service. Restarting on every push tears down the Connections
  # singleton (Kite + Dhan + Groww), the KiteTicker WebSocket, every in-process
  # cache (positions, holdings, funds), and the Dhan rate-limit cool-off
  # state — which then triggers the "Token can be generated once every 2
  # minutes" cascade when two Dhan accounts race for re-login on cold start.
  # Operator: "is there anyway to protect api code from redeployment so that
  # connections need not be reset frequently."
  #
  # Strict whitelist — if ANYTHING under backend/, webhook/notify_*, or the
  # systemd unit file changed, we do a full restart (safe). Otherwise treat
  # the push as frontend-only: skip the kite_tokens.json wipe AND skip the
  # systemctl restart. The new SvelteKit build (already done above) is served
  # from disk on the next request without any service interaction — Litestar
  # reads static files per-request, no content caching.
  BACKEND_TOUCHED=false
  if echo "$CHANGED" | grep -qE '^(backend/|webhook/notify_|etc/systemd/)'; then
      BACKEND_TOUCHED=true
  fi
  DEPLOY_TYPE=$([ "$BACKEND_TOUCHED" = "true" ] && echo "full" || echo "fe-only")
  echo "[$TS] Deploy type: $DEPLOY_TYPE"

  # ─── conn_service vs api: independent restart decisions ─────────────
  # ramboq_conn.service owns the broker sessions (Kite + Dhan + Groww)
  # in a SEPARATE process from ramboq_api. The whole point of the split
  # is that ramboq_api can restart for backend code changes without
  # tearing down broker auth.
  #
  # Code paths that affect the connection service — everything under
  # backend/brokers/ (Broker ABC + adapters + connections + the
  # service + the client + kite_ticker) OR the systemd unit itself.
  # Post-reorg this collapses to one prefix.
  #
  # When ONLY non-conn backend code changes (routes/, algo/, models, etc.),
  # we restart ramboq_api but leave ramboq_conn alone — broker sessions
  # stay warm across the api code update.
  #
  # Only fires on prod ($ENV=prod) because the conn_service unit lives
  # at /opt/ramboq (prod path). Dev pushes (/opt/ramboq_dev) NEVER
  # restart ramboq_conn — dev shares the prod UDS. conn-related code
  # changes only take effect after merging to main.
  CONN_TOUCHED=false
  if [ "$ENV" = "prod" ] && echo "$CHANGED" | grep -qE '^(backend/brokers/|webhook/ramboq_conn\.service)'; then
      CONN_TOUCHED=true
  fi
  echo "[$TS] conn_service restart needed: $CONN_TOUCHED"

  # ─── Skip pip install when no Python dep file changed ──────────────────
  # pip install --no-cache-dir takes ~20-40s on this box even when everything
  # is already installed because it re-resolves the entire dependency tree.
  # Only fire when one of the requirements files actually changed.
  DEPS_CHANGED=false
  if echo "$CHANGED" | grep -qE '^backend/requirements.*\.txt$'; then
      DEPS_CHANGED=true
  fi

  # --- Prod-only: sync nginx configs ---
  if [ "$ENV" = "prod" ]; then
    if echo "$CHANGED" | grep -q '^etc/'; then
      echo "[$TS] Syncing nginx configs..."
      sudo cp -r "$APP_ROOT/etc/nginx/sites-available/." /etc/nginx/sites-available/
      if sudo nginx -t; then
        sudo systemctl reload nginx
      else
        echo "[$TS] ERROR: nginx config test failed — not reloading"
      fi
    fi
  fi

  # --- Install Python deps + build SvelteKit frontend ---
  source venv/bin/activate

  # Install Python dependencies (API layer only) — only when a
  # requirements file actually changed in this push.
  if [ "$DEPS_CHANGED" = "true" ]; then
      pip install --no-cache-dir -r backend/requirements.txt -r backend/requirements-api.txt \
        && echo "[$TS] Python deps installed" \
        || { echo "[$TS] ERROR: pip install failed"; exit 1; }
  else
      echo "[$TS] No requirements.txt change — skipping pip install"
  fi

  # Build SvelteKit frontend
  #
  # nice -n 19 (lowest scheduling priority) + ionice -c 3 (idle I/O class)
  # so vite build yields to the running API process. With a 2-vCPU box,
  # a default-priority build starves the API for the 30-60s build window
  # and every page request stalls. The build still completes in roughly
  # the same wall-clock time when the host is otherwise idle; only the
  # under-load case improves.
  if command -v npm &>/dev/null && [ -f "$APP_ROOT/frontend/package.json" ]; then
    echo "[$TS] Building SvelteKit frontend (low priority)..."
    cd "$APP_ROOT/frontend"
    nice -n 19 ionice -c 3 npm install --prefer-offline 2>&1 | tail -3
    nice -n 19 ionice -c 3 npm run build \
      && echo "[$TS] SvelteKit build complete" \
      || echo "[$TS] WARNING: SvelteKit build failed (non-fatal)"
    cd "$APP_ROOT"
  else
    echo "[$TS] npm not found or no frontend — skipping SvelteKit build"
  fi

  # Fix ownership — manual SSH operations (builds, git) may leave root-owned files
  # that block the next www-data deploy. Fix .svelte-kit, build, node_modules, .git, .log.
  sudo chown -R www-data:www-data "$APP_ROOT/.git" "$APP_ROOT/.log" \
    "$APP_ROOT/frontend/.svelte-kit" "$APP_ROOT/frontend/build" \
    "$APP_ROOT/frontend/node_modules" 2>/dev/null || true

  # ─── Restart path (BACKEND_TOUCHED only) ──────────────────────────────
  PORT=$([ "$ENV" = "prod" ] && echo 8000 || echo 8001)
  DEPLOY_STATUS="fail"
  if [ "$BACKEND_TOUCHED" = "true" ]; then
      # ramboq_conn restart only when connection-layer code actually
      # changed (CONN_TOUCHED). Most backend pushes (route logic, agent
      # rules, etc.) don't touch broker auth and leave ramboq_conn warm.
      if [ "$CONN_TOUCHED" = "true" ]; then
          # If the systemd unit file itself changed in this push, sync
          # the installed copy + daemon-reload BEFORE restart so the
          # new ExecStart / Environment / etc. take effect. Without
          # this, restart re-launches with the stale unit.
          if echo "$CHANGED" | grep -q '^webhook/ramboq_conn\.service$'; then
              echo "[$TS] ramboq_conn.service unit changed — syncing + daemon-reload"
              sudo cp "$APP_ROOT/webhook/ramboq_conn.service" \
                      /etc/systemd/system/ramboq_conn.service 2>&1 || \
                  echo "[$TS] WARN: failed to copy ramboq_conn.service unit"
              sudo systemctl daemon-reload
          fi
          # Conn-layer code changed — clear stale Kite tokens BEFORE
          # restart so the fresh process re-auths cleanly.
          rm -f "$APP_ROOT/.log/kite_tokens.json" 2>/dev/null || true
          echo "[$TS] Restarting ramboq_conn.service (conn-layer change)..."
          sudo systemctl restart ramboq_conn.service 2>&1 || echo "[$TS] WARN: ramboq_conn restart failed (might not be installed yet)"
      else
          echo "[$TS] Preserving ramboq_conn.service — broker sessions stay warm"
      fi

      echo "[$TS] Restarting $API_SERVICE..."
      sudo systemctl restart "$API_SERVICE" || echo "[$TS] ERROR: failed to restart $API_SERVICE"

      # D5 — Health check: verify the service actually came up before declaring success.
      # Tries /api/health first; falls back to / (SPA shell). Both return 200 when
      # Litestar is up. Uvicorn binds locally to 8000 (prod) / 8001 (dev) —
      # CLAUDE.md's "8502/8503" labels are the public-facing identifiers via nginx,
      # not the in-process ports a curl from the same host can reach.
      for i in 1 2 3 4 5 6; do
          if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1 || \
             curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
              DEPLOY_STATUS="ok"
              echo "[$TS] Health check OK on port $PORT (attempt $i)"
              break
          fi
          sleep 5
      done
      if [ "$DEPLOY_STATUS" = "fail" ]; then
          echo "[$TS] ERROR: Health check failed — service did not respond on port $PORT after 6 attempts"
      fi
  else
      # Frontend-only push — preserve broker sessions + KiteTicker + caches.
      # Litestar serves the new frontend/build/ static files on the next
      # request automatically. No service interaction needed. ramboq_conn
      # is similarly untouched.
      echo "[$TS] FE-only push — skipping service restart"
      echo "[$TS] Preserved: ramboq_conn broker sessions, ramboq_api state, KiteTicker, in-process caches, Dhan cool-off"
      # Light health check — verify the new build is in place AND the
      # already-running service still responds.
      if [ -f "$APP_ROOT/frontend/build/index.html" ] && \
         curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
          DEPLOY_STATUS="ok"
          echo "[$TS] FE-only health OK: new build present, running service responsive"
      else
          echo "[$TS] ERROR: FE-only health failed — missing build artefact or service unresponsive"
      fi
  fi

  # D11 — Write last_deploy.json for /admin/health to surface deploy info.
  LAST_DEPLOY_JSON="$APP_ROOT/.log/last_deploy.json"
  cat > "$LAST_DEPLOY_JSON" <<JSONEOF
{
  "branch": "$BRANCH",
  "commit": "$(git rev-parse --short HEAD)",
  "commit_full": "$(git rev-parse HEAD)",
  "subject": "$(git log -1 --format=%s | sed 's/"/\\"/g')",
  "ts": "$TS",
  "status": "$DEPLOY_STATUS"
}
JSONEOF
  sudo chown www-data:www-data "$LAST_DEPLOY_JSON" 2>/dev/null || true

  echo "[$TS] Sending startup notification..."
  python "$APP_ROOT/webhook/notify_deploy.py" \
      --status "$DEPLOY_STATUS" \
      --branch "$BRANCH" \
      --commit "$(git rev-parse --short HEAD)" \
      --deploy-type "$DEPLOY_TYPE" \
      && echo "[$TS] Startup notification done" \
      || echo "[$TS] WARNING: startup notification failed"

  echo "[$TS] Deployment complete (HEAD: $(git rev-parse --short HEAD))"

  # Release the host-wide lock before running metrics capture.
  # capture_metrics.py can take 10-20 min; holding the lock for its
  # duration blocks any concurrent deploy (e.g. prod waiting on dev).
  # All safety-critical steps (pull, build, restart, health check,
  # notification) are done above — metrics are best-effort only.
  exec 200>&-

  # D12 — Capture per-release code metrics. Best-effort; never fails
  # the deploy. Main branch deploys land under `git describe`, dev
  # branches use `dev-<short-sha>` so the trend chart doesn't mix
  # release rows with experimental ones. radon / vulture must be
  # `pip install`-ed alongside the API venv (already done in
  # requirements-api.txt as of Jun 2026).
  if [ "$DEPLOY_TYPE" != "frontend-only" ] && [ "$DEPLOY_STATUS" = "ok" ]; then
      if [ "$BRANCH" = "main" ]; then
          METRICS_TAG="$(git describe --tags --abbrev=0 2>/dev/null || echo "main-$(git rev-parse --short HEAD)")"
      else
          METRICS_TAG="dev-$(git rev-parse --short HEAD)"
      fi
      echo "[$TS] Capturing code metrics under tag $METRICS_TAG (lock released)..."
      "$APP_ROOT/venv/bin/python" "$APP_ROOT/scripts/capture_metrics.py" \
          --release-tag "$METRICS_TAG" --force --with-test-times \
          && echo "[$TS] Code metrics captured" \
          || echo "[$TS] WARNING: code-metrics capture failed (deploy itself unaffected)"
  fi
} >> "$LOG" 2>&1
