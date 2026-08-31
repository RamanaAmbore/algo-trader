# Plan: Fix P.Close=0 for MCX options in derivatives Legs/Exp-close grids

## Task
MCX option positions (e.g. CRUDEOIL-17SEP26-7900-PE) show P.Close=0 in the derivatives
Legs and Exp-close grids. Root cause: `_override_stale_close_from_snapshot` queries
`daily_book.ltp` only for snapshots captured BEFORE today_open (08:00 IST = 02:30 UTC).
MCX option snapshots are captured DURING the MCX session (18:30 UTC onward) — after the
cutoff — so the first pass finds no rows for them and `previous_close` stays at 0.

The fix: add a second-pass fallback query that reads `daily_book.previous_close` (the
prior-session settlement stored in each snapshot row) for any positions still at
`previous_close=0` after the first pass. No time cutoff needed because
`daily_book.previous_close` is always the prior-session settlement regardless of when
the snapshot was captured. Verified in dev DB: CRUDEOIL26SEP7900PE has
`previous_close=214.6` at captured_at=2026-08-31 18:30 UTC (correct Aug 30 settlement).

## Agents
- backend: Add second-pass fallback in `_override_stale_close_from_snapshot` in
  `backend/api/routes/positions.py`. After the first pass (reads daily_book.ltp WHERE
  captured_at < today_open), collect all (account, tradingsymbol) pairs where
  `raw['previous_close']` is still 0. If any, do a second async DB query:
  `SELECT DISTINCT ON (account, symbol) account, symbol, previous_close FROM daily_book
   WHERE kind='positions' AND previous_close IS NOT NULL AND previous_close > 0
     AND (account, symbol) IN (...)
   ORDER BY account, symbol, captured_at DESC`
  For each matching row, set raw['previous_close'] and raw['close_price'] from
  daily_book.previous_close. Collect these as patched_idx2 and recompute
  day_change_val, day_change, day_change_percentage, pnl_percentage exactly as the
  first pass does (same vectorised recompute block). Log: "positions: close-override
  second-pass (MCX option fallback) patched N rows from daily_book.previous_close".
  NOTE: do NOT change the existing first-pass query or its time filter. This is purely
  additive.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add 2 test cases to `TestOverrideStaleCloseFromSnapshot` in
  `backend/tests/test_positions_route.py`:
  1. `test_mcx_option_no_within_window_snapshot_fallback_to_daily_book_previous_close`:
     First execute returns [] (no within-window snapshot). Second execute returns
     [("ZG0790", "CRUDEOIL26SEP7900PE", 214.6)]. Assert previous_close=214.6,
     close_price=214.6, day_change_val recomputed.
  2. `test_mcx_option_first_pass_wins_over_fallback`:
     First execute returns [("ZG0790", "CRUDEOIL26SEP7900PE", 220.0, None)].
     Second execute should NOT be called (previous_close was set by first pass).
     Assert previous_close=220.0 (first-pass value, not overridden).
  Update `_run_close_override` helper (or add `_run_close_override_two_pass`) to accept
  optional second_pass_rows list. Use side_effect=[first_result, second_result] on
  mock_session.execute when second_pass_rows is provided.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(positions): second-pass fallback reads daily_book.previous_close for MCX options where no within-window snapshot exists

## Done when
MCX option positions (CRUDEOIL-17SEP26-7900-PE etc.) show correct P.Close in derivatives
Legs/Exp-close grids. `previous_close` set from daily_book.previous_close for MCX option
rows where captured_at > today_open excluded them from first pass. Tests green.
