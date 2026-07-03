#!/bin/bash
# perf_capture_run.sh — Run the Playwright runtime-capture spec against a
# deployed dev server and emit .log/perf_capture_<utc>.json (+ latest).
#
# We deliberately target the DEPLOYED dev URL (dev.ramboq.com) rather
# than spinning up a local dev server: the deploy is already warm, the
# DB is populated with realistic data, and `ensureAuth` works exactly
# the same as any other e2e spec. Local `npm run dev` startup + login
# adds ~30-60 s and gives colder numbers.
#
# Prereqs (fail-fast at the top so you don't wait 5 min for a bad run):
#   - $PLAYWRIGHT_USER / $PLAYWRIGHT_PASS in env (real dev creds)
#   - dev.ramboq.com reachable
#   - frontend/ deps installed (npx playwright already installed)
#
# Env knobs:
#   PLAYWRIGHT_BASE_URL   default https://dev.ramboq.com
#   PLAYWRIGHT_PROJECT    default chromium-desktop
#   RAMBOQ_COMMIT         short SHA recorded in output (default: git HEAD)
#   PERF_CAPTURE_QUIET    if set, suppress the summary at the end
#
# Exit 0 on success; non-zero on any hard error (creds missing, network
# down, spec failed). The output JSON always lands even if individual
# page captures error — the spec's afterAll writes whatever it collected.

set -euo pipefail

# Repo root (this script lives at scripts/).
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BASE_URL="${PLAYWRIGHT_BASE_URL:-https://dev.ramboq.com}"
PROJECT="${PLAYWRIGHT_PROJECT:-chromium-desktop}"
LOG_DIR="$ROOT/.log"

# ── Prereq checks ─────────────────────────────────────────────────────────

if [[ -z "${PLAYWRIGHT_USER:-}" || -z "${PLAYWRIGHT_PASS:-}" ]]; then
  echo "[perf_capture_run] ERROR: PLAYWRIGHT_USER / PLAYWRIGHT_PASS must be set." >&2
  echo "                   These are the real dev.ramboq.com credentials." >&2
  echo "                   The e2e auth fixture ($ROOT/frontend/e2e/fixtures/auth.js)" >&2
  echo "                   defaults to rambo/admin1234 which only work on localhost." >&2
  exit 2
fi

echo "[perf_capture_run] target: $BASE_URL  project: $PROJECT"

# Reachability probe. Use GET on /signin (a real page) instead of HEAD on
# root — HEAD is rejected by many app-tier frameworks with 405 even when
# the server is fully healthy. -f fails on 4xx/5xx so a genuine "cluster
# down" state exits early instead of waiting for Playwright's own timeout.
if ! curl -sSf -m 10 -o /dev/null "$BASE_URL/signin"; then
  echo "[perf_capture_run] ERROR: $BASE_URL/signin not reachable." >&2
  exit 3
fi

# Optional commit stamp — Playwright spec reads $RAMBOQ_COMMIT.
if [[ -z "${RAMBOQ_COMMIT:-}" ]]; then
  export RAMBOQ_COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo '')"
fi

mkdir -p "$LOG_DIR"

# ── Run Playwright ────────────────────────────────────────────────────────

pushd "$ROOT/frontend" > /dev/null
PLAYWRIGHT_BASE_URL="$BASE_URL" \
  npx playwright test e2e/perf_capture.spec.js \
    --project="$PROJECT" \
    --workers=1 \
    --reporter=line
capture_rc=$?
popd > /dev/null

if [[ $capture_rc -ne 0 ]]; then
  echo "[perf_capture_run] WARN: spec exited $capture_rc — output may be partial." >&2
fi

# ── Summarise ─────────────────────────────────────────────────────────────

latest="$LOG_DIR/perf_capture_latest.json"
if [[ ! -f "$latest" ]]; then
  echo "[perf_capture_run] ERROR: no perf_capture_latest.json found; spec probably" >&2
  echo "                   crashed before afterAll wrote output." >&2
  exit 4
fi

if [[ -n "${PERF_CAPTURE_QUIET:-}" ]]; then
  exit 0
fi

echo ""
echo "[perf_capture_run] wrote $(realpath --relative-to="$ROOT" "$latest" 2>/dev/null || echo "$latest")"
echo "[perf_capture_run] per-page runtime summary:"

# Python one-liner — jq isn't available on every dev machine.
"$ROOT/venv/bin/python" - "$latest" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
pages = data.get("frontend", {}).get("pages", {})
print(f"  {'route':<28} {'LCP':>8} {'TBT':>8} {'heap':>8} {'ws':>4}  {'refresh':>8}")
for route, row in pages.items():
    r = row.get("runtime", {})
    if "error" in r:
        print(f"  {route:<28} ERROR: {r['error'][:60]}")
        continue
    def fmt_ms(v):  return f"{v}ms" if v is not None else "-"
    def fmt_mb(v):  return f"{v}MB" if v is not None else "-"
    def fmt_int(v): return f"{v}" if v is not None else "-"
    print(f"  {route:<28} "
          f"{fmt_ms(r.get('lcp_ms')):>8} "
          f"{fmt_ms(r.get('tbt_ms', r.get('long_task_ms'))):>8} "
          f"{fmt_mb(r.get('heap_mb')):>8} "
          f"{fmt_int(r.get('ws_connections')):>4}  "
          f"{fmt_ms(r.get('refresh_click_ms')):>8}")
PY
