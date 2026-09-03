# Plan: Settings audit — close all gaps, seed notification caps to DB

## Context
Dev alerts (Telegram, email) were leaking to prod channels because dev DB had
`telegram_enabled=true` which overrides the YAML `cap_in_dev` default. A full 4-layer
audit (YAML → SEEDS → DB → UI) revealed 4 additional structural gaps beyond the
notification cap issue. All fixed in one commit.

YAML remains as the resilience fallback when DB is unreachable. DB is the runtime
authority when reachable. SEEDS adds `dev_default` support so dev environments seed
correctly on first boot without manual DB surgery.

## Gaps to fix

### G1 — Notification caps: add 6 missing caps to SEEDS (main fix)
`agent_alerts`, `market_summary`, `ntfy`, `visitor_report`, `market_feed`, `genai`
are YAML-only today — no DB row, no UI toggle. Add them to SEEDS with `dev_default=false`.
Add `dev_default` field support to `seed_settings()`.

### G2 — 6 code keys not in SEEDS at all (P0)
These keys are read in code via `get_int`/`get_float` but have no DB row — invisible in UI:
- `templates.tp_limit_tick_offset_nfo` — template_attach.py, float, default 0.05
- `templates.tp_limit_tick_offset_default` — template_attach.py, float, default 0.5
- `templates.wing_min_oi_hard_reject` — template_attach.py, int, default 0
- `orders.open_order_watchdog_seconds` — background.py, int, default 300
- `retention.list_funds_hard_cap` — routes/history.py, int, default 1000
- `alerts.fire_at_window_sec` — agent_engine.py, int, default 360
Note: code uses `template.` (singular) prefix for the first 3; rename to `templates.` in both SEEDS and call sites.

### G3 — orders.default_account seed default wrong (P1)
SEEDS line 514 defaults to `"ZG0790"` — hardcodes a prod account ID. New dev envs
would seed this wrong. Fix: change default to `""` (empty string = auto-pick).

### G4 — execution.dev_active type inconsistency (P2)
SEEDS stores default as Python string `"false"` but annotates type as `"bool"`.
Functionally fine (get_bool lowercases), but inconsistent. Normalise to lowercase
string `"false"` (already correct) — verify no type mismatch in seed_settings().

## Agents

- backend: Changes to `backend/shared/helpers/settings.py`:
  1. Add `dev_default` field support in `seed_settings()`: when `deploy_branch != 'main'`,
     use `seed.get("dev_default", seed["default"])` for insert-if-absent value.
  2. Add 6 notification cap SEEDS entries (category: notifications) with `dev_default="false"`:
     - `notifications.agent_alerts_enabled` — prod default: true — "Agent alert dispatch"
     - `notifications.market_summary_enabled` — prod default: true — "Market open/close summaries"
     - `notifications.ntfy_enabled` — prod default: true — "ntfy.sh push notifications"
     - `notifications.visitor_report_enabled` — prod default: true — "Nightly visitor report"
     - `notifications.market_feed_enabled` — prod default: true — "Market news feed"
     - `notifications.genai_enabled` — prod default: true — "GenAI market snapshot"
  3. Add 6 missing code-used keys to SEEDS (G2):
     - `templates.tp_limit_tick_offset_nfo` (float, 0.05)
     - `templates.tp_limit_tick_offset_default` (float, 0.5)
     - `templates.wing_min_oi_hard_reject` (int, 0)
     - `orders.open_order_watchdog_seconds` (int, 300)
     - `retention.list_funds_hard_cap` (int, 1000)
     - `alerts.fire_at_window_sec` (int, 360)
  4. Fix `orders.default_account` default from `"ZG0790"` to `""` (G3).
  5. In `backend/api/algo/template_attach.py`: rename `get_float("template.tp_limit_tick_offset_nfo")` →
     `get_float("templates.tp_limit_tick_offset_nfo")` (and the other 2 template. keys) (G2 rename).

- frontend: The settings page auto-renders all SEEDS grouped by category — verify
  new keys appear under the correct category headers. No template change needed
  unless a category label needs adjustment.

- broker: skip
- doc: skip

- backend-test: Add to `backend/tests/test_cap_flags_dev.py`:
  - `test_seed_uses_dev_default_on_non_main` — mock `deploy_branch=workshop`, verify
    `agent_alerts_enabled` seeds as `"false"` not `"true"`
  - `test_seed_uses_default_on_main` — mock `deploy_branch=main`, verify seeds as `"true"`
  - `test_is_enabled_reads_new_caps_from_db` — verify `is_enabled('agent_alerts')` and
    `is_enabled('market_summary')` read the DB value after seeding

- playwright: skip

## Critical files
- `backend/shared/helpers/settings.py` — SEEDS list + `seed_settings()` function
- `backend/api/algo/template_attach.py` — rename `template.` → `templates.` (3 call sites)
- `backend/shared/helpers/utils.py` — `is_enabled()` — no change needed (DB-first already works)
- `frontend/src/routes/(algo)/admin/settings/+page.svelte` — verify new keys auto-render

## Immediate DB fix (SSH — first step in impl, before code ships)

### Dev server (`ramboq_dev`):
```sql
UPDATE settings SET value = 'false'
WHERE key IN (
  'notifications.telegram_enabled',
  'notifications.email_enabled',
  'notifications.notify_on_deploy',
  'simulator.notify_during_run'
);
```

### Prod server (`ramboq`):
```sql
UPDATE settings SET value = 'false'
WHERE key = 'simulator.notify_during_run';
```
(Fixes capital-F `False` → lowercase `false` for consistency.)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(settings): close audit gaps — seed all notification caps to DB, add 6 missing keys, fix account default

## Done when
- All 10 notification caps visible and toggleable in `/admin/settings` UI
- Dev server: telegram/email/notify_on_deploy/simulator.notify_during_run = false
- `seed_settings()` uses `dev_default` on non-main branches
- 6 code-used keys (`templates.tp_*`, `orders.open_order_watchdog_seconds`, `retention.list_funds_hard_cap`, `alerts.fire_at_window_sec`) visible in UI
- `orders.default_account` SEEDS default = `""` (not `"ZG0790"`)
- `template.` → `templates.` rename clean in template_attach.py
- pytest green, svelte-check 0 errors
