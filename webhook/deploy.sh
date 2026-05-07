#!/bin/bash
# Unified deploy script — handles prod and dev environments.
# Called by /etc/webhook/dispatch.sh with ENV and REF arguments.
# Usage: deploy.sh <ENV> <REF>
#   ENV : prod | dev
#   REF : refs/heads/<branch>  (e.g. refs/heads/main)
#
# Services run uvicorn (Litestar API) + SvelteKit SPA as static files.
# The SvelteKit build (frontend/build/) is served as static files by Litestar.

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

{
  # Abort on any error so a failed git pull doesn't get masked by a successful
  # restart further down. The previous version swallowed git failures and
  # reported "Deployment complete" even when the working tree was stale.
  set -e

  echo "[$TS] Deploy triggered: $ENV (branch: $BRANCH)"
  echo "Running as: $(whoami)"

  cd "$APP_ROOT" || { echo "[$TS] ERROR: cannot cd to $APP_ROOT"; exit 1; }

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

  # Install Python dependencies (API layer only)
  pip install --no-cache-dir -r backend/requirements.txt -r backend/requirements-api.txt \
    && echo "[$TS] Python deps installed" \
    || { echo "[$TS] ERROR: pip install failed"; exit 1; }

  # Build SvelteKit frontend
  if command -v npm &>/dev/null && [ -f "$APP_ROOT/frontend/package.json" ]; then
    echo "[$TS] Building SvelteKit frontend..."
    cd "$APP_ROOT/frontend"
    npm install --prefer-offline 2>&1 | tail -3
    npm run build \
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

  # Clear stale Kite token cache — forces fresh login after deploy
  rm -f "$APP_ROOT/.log/kite_tokens.json" 2>/dev/null || true

  echo "[$TS] Restarting $API_SERVICE..."
  sudo systemctl restart "$API_SERVICE" || echo "[$TS] ERROR: failed to restart $API_SERVICE"

  echo "[$TS] Sending startup notification..."
  python "$APP_ROOT/webhook/notify_deploy.py" \
    && echo "[$TS] Startup notification done" \
    || echo "[$TS] WARNING: startup notification failed"

  echo "[$TS] Deployment complete (HEAD: $(git rev-parse --short HEAD))"
} >> "$LOG" 2>&1
