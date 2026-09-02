# Plan: Disable market summary and visitor reports on dev

## Context
Market open/close summaries and the nightly visitor report are currently firing on
dev.ramboq.com. The cap_in_dev system already suppresses telegram, mail, and ntfy on
dev — but the market summary (`_perf_fire_open_summary` / `_run_close_summary_once`) and
visitor report (`_task_visitor_log_daily`) do not check any per-feature capability flag,
so they run unconditionally regardless of the channel flags. The fix adds two new
capability flags and gates the relevant code paths with them.

## Task
1. `backend/config/backend_config.yaml` — add two flags to `cap_in_dev`:
   ```yaml
   market_summary:  False  # open + close performance summaries (dev: off)
   visitor_report:  False  # nightly nginx visitor digest (dev: off)
   ```
   No change to `cap_in_prod` (both are implicitly True there — missing key defaults to True).

2. `backend/api/background.py` — add `is_enabled()` guards in three places:
   - `_perf_fire_open_summary()` (~line 626): early-return if `not is_enabled('market_summary')`
   - `_run_close_summary_once()` (~line 5665): early-return if `not is_enabled('market_summary')`
   - `_task_visitor_log_daily._run_once()` (~line 4267): early-return if `not is_enabled('visitor_report')`

   Import `is_enabled` from `backend.shared.helpers.utils` (already imported in visitor task,
   needs to be added locally in the other two functions or hoisted to the top of each).

## Agents
- backend: Apply all changes above to backend_config.yaml and background.py
- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip (no new logic — guards are trivially correct; existing cap flag tests cover is_enabled)
- playwright: skip

## Tests
- pytest: no
- svelte-check: no
- playwright: no

## Commit message
fix(dev): disable market summary + visitor report on dev via cap_in_dev flags

## Done when
- dev.ramboq.com no longer sends market open/close summaries or visitor reports
- prod.ramboq.com behaviour unchanged
- `is_enabled('market_summary')` and `is_enabled('visitor_report')` toggleable from /admin/settings
