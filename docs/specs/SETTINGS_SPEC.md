# Settings Specification

Single source of truth for runtime-tunable configuration accessed by operators via
`/admin/settings`. Settings are DB-backed with a three-tier fallback (cache → YAML →
in-code default) for zero-downtime config changes.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/routes/settings.py` · `backend/api/models.py` · 
`backend/config/backend_config.yaml` · 
`frontend/src/routes/(algo)/admin/settings/+page.svelte`

---

## Contents

1. [Architecture and Tiers](#1-architecture-and-tiers)
2. [Setting Buckets](#2-setting-buckets)
3. [Endpoints](#3-endpoints)
4. [Admin Page](#4-admin-page)
5. [Seeding and Migration](#5-seeding-and-migration)
6. [Live Effects](#6-live-effects)
7. [Edge Cases](#7-edge-cases)
8. [Test Coverage Map](#8-test-coverage-map)

---

## 1. Architecture and Tiers

**Three-tier read chain** (in priority order):

| Tier | Source | TTL | Fallback |
|---|---|---|---|
| 1 | In-memory LRU cache | 5 min | Tier 2 |
| 2 | PostgreSQL `settings` table | None | Tier 3 |
| 3 | `backend/config/backend_config.yaml` | None | In-code default |

**Read path**: Application calls `settings.get("alerts.cooldown_minutes")` →
check Tier 1 cache → miss → hit Tier 2 DB query → cache result (5min TTL) →
return to caller. On DB miss, fall back to YAML; if YAML omitted, use Python
default defined in the calling module.

**Write path**: Operator uses `/admin/settings` page to PATCH a key → backend
validates value + type → writes to DB → invalidates Tier 1 cache → live-effect
handlers re-apply (if applicable) → response reflects new value immediately.

**Immutability**: YAML and in-code defaults never modified at runtime. Only the DB
table accepts writes. Allows config-as-code discipline (YAML is source of truth,
DB overrides are temporary via the UI).

---

## 2. Setting Buckets

**Canonical buckets** (settable via UI):

### alerts.*
Alert delivery and rate-limiting gates.

| Key | Type | Default | Description |
|---|---|---|---|
| alerts.cooldown_minutes | INT | 30 | Silence duplicate alerts from same agent for N minutes |
| alerts.baseline_offset_min | INT | 15 | Minimum age (minutes) of a performance data point before loss-alert check |
| alerts.rate_window_min | INT | 10 | Rolling window (minutes) for rate limiting (max M alerts per window) |

### performance.*
Backend refresh cadence and summary timing.

| Key | Type | Default | Description |
|---|---|---|---|
| performance.refresh_interval_s | INT | 30 | Poll cycle interval (seconds) for positions/holdings/funds |
| performance.open_summary_offset_minutes | INT | 5 | Minutes after market open to fire open_summary snapshot |
| performance.close_summary_offset_minutes | INT | 15 | Minutes after market close to fire close_summary snapshot |
| performance.nav_snapshot_time_ist | STRING | 16:00 | Hourly time (IST) for daily NAV computation |

### simulator.*
Simulated-mode parameters (applies to SIM execution mode only).

| Key | Type | Default | Description |
|---|---|---|---|
| simulator.ticks_per_second | INT | 10 | Tick generation rate in simulator |
| simulator.auto_stop_on_circuit | BOOL | true | Stop sim on circuit breaker (broker error) |
| simulator.speed_multiplier | FLOAT | 1.0 | Simulation time speedup (1.0 = real-time) |

### notifications.*
Alert delivery channels and deployment notifications.

| Key | Type | Default | Description |
|---|---|---|---|
| notifications.telegram_enabled | BOOL | true | Enable Telegram delivery for alerts |
| notifications.email_enabled | BOOL | true | Enable email delivery for alerts |
| notifications.deploy_notify_channels | STRING | "" | Comma-separated Telegram chat IDs for deploy notifications |

### logging.*
Log verbosity (live-effect fields — changes take effect without restart).

| Key | Type | Default | Description |
|---|---|---|---|
| logging.file_log_level | STRING | INFO | File log verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| logging.console_log_level | STRING | WARNING | Console handler verbosity |
| logging.error_log_level | STRING | ERROR | Error file handler verbosity |

### hedge_proxies.*
Regression parameters for proxy-hedge computation.

| Key | Type | Default | Description |
|---|---|---|---|
| hedge_proxies.regression_lookback_days | INT | 60 | Lookback window (trading days) for beta regression |
| hedge_proxies.regression_min_bars | INT | 15 | Minimum bars required for valid regression (else reject) |
| hedge_proxies.regression_max_age_days | INT | 30 | Re-run regression if older than N days |

### cap_in_dev
Capability flag overrides for development/testing only. **Dict structure** (not string).

```yaml
cap_in_dev:
  trade_live_orders: false        # Disable live trading on dev branch
  access_investor_portal: true    # Enable LP token portal test
  allow_data_backfill: true       # Allow manual OHLCV backfill
```

---

## 3. Endpoints

**`GET /api/admin/settings`** (admin-guarded)

Returns all settings grouped by bucket. Response shape:

```json
{
  "buckets": {
    "alerts": [
      {"key": "alerts.cooldown_minutes", "value_type": "INT", "value": "30", "default_value": "30", "description": "..."},
      ...
    ],
    "performance": [...],
    "simulator": [...],
    ...
  }
}
```

**`GET /api/admin/settings/{key}`** (admin-guarded)

Fetch a single setting + metadata.

```json
{
  "id": 42,
  "category": "alerts",
  "key": "alerts.cooldown_minutes",
  "value_type": "INT",
  "value": "30",
  "default_value": "30",
  "description": "Silence duplicate alerts from same agent for N minutes",
  "schema": null,
  "units": "minutes",
  "updated_at": "2026-07-10T15:30:45Z"
}
```

**`PATCH /api/admin/settings/{key}`** (admin-guarded)

Update a single setting. Request body:

```json
{
  "value": "45"
}
```

Validation runs per value_type + optional JSON schema. Response reflects new value
immediately + live-effect handlers re-apply (e.g., log level change takes effect
without restart).

**`POST /api/admin/settings/{key}/reset`** (admin-guarded)

Reset a setting back to its YAML default_value. Deletes the DB row (if present) so
next read falls back to YAML.

---

## 4. Admin Page

**`/admin/settings`** — Grouped card layout, one card per bucket.

**Card features**:
- Bucket title + summary (e.g. "Alerts: 3 settings")
- Per-setting row: key · value_type · input field · units · description
- Reset button per setting (reverts to YAML default)
- Save button (async PATCH, shows loading + success/error toast)
- Read-only mode if operator lacks `manage_settings` cap (future gate)

**Inline edit flow**:
1. Operator types new value into input field
2. Clicks Save (or presses Enter)
3. Frontend validates type (INT must parse, BOOL must be true/false, etc.)
4. POST PATCH /api/admin/settings/{key} with new value
5. Backend validates against value_type + schema
6. Live-effect handlers run (if applicable)
7. Frontend shows success toast + refreshes grid

**Error cases**:
- Invalid type (e.g. "abc" for INT) → 400 Bad Request + error message
- Out-of-range value → 400 Bad Request + schema details
- Unknown key → 404 Not Found
- DB write failure → 500 Internal Server Error

---

## 5. Seeding and Migration

**Initial seed** (on first deploy or empty table):

1. Query YAML for all keys in the defined buckets
2. For each key not in DB, INSERT a row with the YAML value + default_value + schema
3. Idempotent: re-running seed does not overwrite existing rows (ON CONFLICT ... DO NOTHING)

**Retired-key cleanup** (optional, operator-triggered):

When a key is removed from the codebase, the seeder can optionally DELETE its row.
Allows gradual deprecation (key remains in DB until explicitly removed in a future
deploy).

**YAML-first philosophy**: Buckets and keys are defined in `backend/config/backend_config.yaml`
as the source of truth. Frontend + admin page discover settings by reading the list
from the backend (not hardcoded in JavaScript).

---

## 6. Live Effects

**Most settings are read on-demand** — application code calls `settings.get("key")`
on each use. Config changes take effect on the next read (no restart needed).

**Exception: Long-lived handlers** — some settings are captured at import time into
objects that live for the entire process lifetime. Changes to these require
live-effect handlers to re-apply the new value.

**Live-effect dispatch** (`_apply_live_effects(key, value)` in settings.py):

| Key Pattern | Handler | Effect | Restart Required |
|---|---|---|---|
| logging.* | `_apply_log_level()` | Re-set handler level immediately | No |

**Pattern for new live-effect setting**:
1. Define the key in backend_config.yaml
2. In settings.py, add a case in `_apply_live_effects()`
3. Call the re-apply handler to mutate the long-lived object
4. Log the change for operator visibility

---

## 7. Edge Cases

### Invalid value type (e.g., "abc" for INT)
- Validation function rejects with 400 Bad Request
- Error message surfaces the expected type + schema constraints
- DB row not written; cached value unchanged

### Unknown key (typo in operator input)
- Backend returns 404 Not Found
- Frontend does not offer a "create new setting" UI (prevent ad-hoc keys)
- Operator must use YAML to add new settings (code review + deploy)

### Missing YAML default (key exists in DB but not in YAML)
- Treated as normal — DB value is authoritative
- Fallback to in-code default (if known) or empty string

### Tier 1 cache stale (operator updates, then immediately reads old value)
- Cache TTL is 5 minutes; reads within 5min of write may hit stale cache
- Acceptable trade-off (stale window is short, consistency is eventual)
- If operator requires immediate visibility, reload the page (clears browser cache)

### cap_in_dev (dict structure, development-only)
- Stored as JSON in the DB (value_type = "JSON")
- `cap_in_dev.trade_live_orders = false` disables live-order caps on dev branch
- Non-dev branches ignore this key (no effect on production)
- Preserves backward compatibility (alert_* settings preserved via key prefix match)

### Settings lost in config file deletion
- If backend_config.yaml is malformed or unreachable, YAML tier fails silently
- Reads fall back to DB tier (live value preserved even if YAML is broken)
- Operator should fix the YAML and redeploy; DB acts as temporary refuge

---

## 8. Test Coverage Map

### Backend — covered

- `test_settings_read_chain.py` — Tier 1 cache → Tier 2 DB → Tier 3 YAML → in-code
- `test_settings_validation.py` — INT, STRING, BOOL, JSON type checks + schema
- `test_settings_live_effect.py` — logging.file_log_level change applied without restart
- `test_settings_crud.py` — PATCH, POST reset, GET single/all
- `test_settings_seed.py` — Initial seed idempotency, retired-key cleanup

### Backend — gaps

- YAML load error (malformed YAML, I/O error) recovery
- Cache eviction + LRU behavior under high frequency
- Concurrent PATCH requests (same key from two tabs) — last write wins?

### Frontend — covered

- `settings_page.spec.js` — Grid renders, inline edit, Save/Reset buttons
- `settings_validation.spec.js` — Form rejects invalid types before submit
- `settings_error_toast.spec.js` — Error messages surface from 400/500 responses

### Frontend — gaps

- Live reload on PATCH success (grid re-fetches to show new value)
- Bucket card collapse/expand state persisted to localStorage
- Read-only mode when operator lacks manage_settings cap

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
