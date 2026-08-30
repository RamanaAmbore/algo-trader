# Plan: Combine morning tasks into single 05:30 wake-up

## Context
Currently three separate early-morning scheduled tasks exist:
- `_task_holiday_refresh` at **04:00 IST** (background.py:2336) — holiday calendar + `load_today_open_time()`
- `_task_token_refresh` at **05:45 IST** (background.py:468) — Kite-only proactive token pre-warm
- **06:00 IST** — Kite token hard-expiry (external Kite event, no code)

Problems:
1. Three separate wake-ups for logically related morning prep
2. `_task_token_refresh` only covers Kite — Dhan and Groww tokens not proactively refreshed
3. Token refresh at 05:45 leaves only 15 min before 06:00 expiry (tight if retry needed)
4. The `@retry_kite_conn` decorator auto-recovers from token expiry on every API call for ALL
   brokers — so the scheduled refresh is best-effort optimization, not a correctness requirement

Design: merge into a single **05:30 IST combined task** with best-effort token refresh for all
broker types. `_task_token_refresh` is removed. `_task_holiday_refresh` absorbs its work and
shifts to 05:30. `_task_perf_snapshot` (04:00) is untouched — separate concern.

## Task
1. Change `_task_holiday_refresh` trigger from 04:00 → 05:30 by updating the
   `holiday_refresh_time` default in `backend/config/backend_config.yaml` from `"04:00"` to
   `"05:30"`. The task already reads this config key.

2. Expand `_task_holiday_refresh` (background.py:2336-2478) to also do proactive token refresh
   for all broker types after the holiday calendar step:
   - Under `RAMBOQ_USE_CONN_SERVICE=0` (direct mode): iterate `Connections().conn`, call
     `get_kite_conn(test_conn=True)` for KiteConnection instances,
     `get_dhan_conn(test_conn=True)` for DhanConnection instances,
     `get_groww_conn(test_conn=True)` for GrowwConnection instances (best-effort, log warning
     on failure, do NOT raise — decorator handles recovery on next API call)
   - Under `RAMBOQ_USE_CONN_SERVICE=1`: skip token refresh (broker layer owns it, same as
     existing `_task_token_refresh` no-op logic)

3. Remove `_task_token_refresh` function (background.py:468-497) and its registration at
   line 5952 (`asyncio.create_task(_supervised(_task_token_refresh, ...))`).

4. Update `BROKER_SPEC.md` to document the combined 05:30 task and removal of the separate
   05:45 Kite-only refresh.

## Agents
- backend: (a) In `backend/config/backend_config.yaml`: change `holiday_refresh_time` default
  from `"04:00"` to `"05:30"`. (b) In `backend/api/background.py`: expand
  `_task_holiday_refresh` to add best-effort token refresh loop (all broker types) after the
  NSE API step; guard with `RAMBOQ_USE_CONN_SERVICE` check same pattern as existing
  `_task_token_refresh`. (c) Remove `_task_token_refresh` function and its `create_task`
  registration. Keep `_task_perf_snapshot` (04:00) and `_task_purge_perf_snapshots` (04:05)
  unchanged.
- backend-test: Update any tests referencing `_task_token_refresh` or the 05:45 trigger time.
  Add a test that verifies `_task_holiday_refresh` calls token refresh for each broker type
  when `RAMBOQ_USE_CONN_SERVICE=0`. Existing holiday refresh tests must still pass.
- doc: Update `docs/specs/BROKER_SPEC.md` — replace the separate 04:00 / 05:45 / 06:00
  entries with the single 05:30 combined task. Document that token refresh is best-effort
  (decorator auto-recovers), covers all three brokers, and is a no-op under
  RAMBOQ_USE_CONN_SERVICE=1.

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
refactor(background): combine morning tasks into single 05:30 wake-up; add all-broker token refresh

## Done when
- `_task_token_refresh` no longer exists in background.py
- `_task_holiday_refresh` fires at 05:30 and calls token refresh for Kite, Dhan, Groww
- `holiday_refresh_time` default is `"05:30"` in backend_config.yaml
- pytest passes; BROKER_SPEC updated
