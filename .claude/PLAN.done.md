# Plan: Fix NavStrip P1 = 0 after closed-hours refresh pollutes latest_batch

## Context

Root cause: `_task_closed_hours_refresh` writes new `daily_book` rows every 30 min
whenever all segments are closed (Saturdays, Sunday, Mon pre-open, weekday evenings).
On Saturday, Kite returns the same `last_price` as Friday's settlement — prices don't
move between sessions. These refresh rows have `captured_at = Saturday 07:30, 08:00…`
but `ltp = Friday settlement LTP`.

`_positions_snapshot()` SQL:
- `latest_batch = MAX(captured_at)` per account → picks Saturday refresh row
- `prev_batch` finds most-recent before `latest_batch` AND `< today_ist_midnight` → picks
  Friday 16:15 settlement (which has the SAME ltp as Saturday's row)
- `computed_day_pnl = (Saturday LTP − Friday LTP) × qty = 0`
- `close_price = prev_ltp = Friday LTP ≈ last_price = Saturday LTP`
- NavStrip P1 = 0

The original fix's `< today_ist_midnight` guard blocks SAME-DAY intraday snapshots from
leaking into `prev_batch` on trading days. But on Saturday, it doesn't help because
`latest_batch` itself is a Saturday refresh row (stale price = Friday LTP), and
`prev_batch` correctly finds Friday settlement (same price).

## Task

Two targeted changes:

1. **`backend/api/background.py`** — Remove `snapshot_daily_book()` call from
   `_task_closed_hours_refresh`. Keep the broker/API cache busting. Settlement writes
   belong exclusively to `_task_daily_snapshot` (fires at 16:15 IST for NSE, 00:15 IST
   for MCX). Without refresh writes, `latest_batch` always anchors to the real
   settlement snapshot.

2. **`backend/api/routes/positions.py`** — Expand `prev_batch` lookback from
   `INTERVAL '2 days'` to `INTERVAL '7 days'`. Current 2-day window fails after any
   4+ day holiday gap (e.g., last settlement > 2 days before `latest_batch.max_at`).
   7 days covers any Indian holiday block.

No holdings change needed — `_holdings_snapshot()` doesn't use a `prev_batch` CTE; it
reads `previous_close` from the column directly (frozen at first write via COALESCE).

## Agents

- backend: In `backend/api/background.py` around line 4321, remove the
  `await snapshot_daily_book()` call and its result logging (lines 4322–4334).
  Keep the cache-busting block (lines 4315–4318: `_raw_cache_invalidate` calls).
  Keep `invalidate("positions")`, `invalidate("holdings")`, `invalidate("funds")`
  calls after the removed block. Remove the now-unused
  `from backend.api.algo.daily_snapshot import snapshot_daily_book` import on
  line 4295 if it's no longer referenced elsewhere in the function.
  In `backend/api/routes/positions.py` line 91, change
  `AND db.captured_at >= lb.max_at - INTERVAL '2 days'`
  to `AND db.captured_at >= lb.max_at - INTERVAL '7 days'`.
  Update the comment on line 66 to reflect the 7-day window.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add/update tests in `backend/tests/test_positions_navstrip_p_slot.py`
  and/or `backend/tests/test_positions_snapshot_prev_ltp.py` covering:
  (a) When `daily_book` has Saturday refresh rows with same LTP as Friday settlement,
      `_positions_snapshot()` must NOT return `day_change_val = 0`
      (validates that `latest_batch` stays on Friday settlement after the fix).
  (b) `prev_batch` lookback reaches back > 2 days (validates 7-day window for holiday gaps).
  Keep existing tests green.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(snapshot): stop closed-hours refresh from polluting latest_batch; expand prev_batch to 7 days

## Done when

- `backend/api/background.py`: `_task_closed_hours_refresh` no longer calls
  `snapshot_daily_book()`; cache busting still runs.
- `backend/api/routes/positions.py`: `prev_batch` lookback = `INTERVAL '7 days'`.
- All existing snapshot tests pass.
- New test: when `daily_book` has Saturday refresh rows (same LTP as Friday), the
  snapshot returns Friday's day P&L (non-zero), not 0.
