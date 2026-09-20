# Plan: Fix snapshot path previous_close — use db.previous_close not db.ltp

## Context
After deploying the COALESCE unification fix, `chg%` across Pulse and Legs became
consistently 0. Root cause: `_positions_snapshot` (market-closed path) incorrectly
sets `previous_close = db.ltp` (today's settlement LTP). The frontend `_ltp` is also
today's settlement → `_ltp === _prev_close` → `day_pnl = 0` → `chg_pct = 0`.

Semantic rule: `ltp` = live price at snapshot capture time. `previous_close` =
value set at 08:00 IST from BHAV copy — a distinct concept. Code must never
derive `previous_close` from `ltp`. `db.previous_close` is the correct column.
MCX slight BHAV deviations are acceptable by design.

## Task
Change one word in `_positions_snapshot` SQL (`positions.py` line 277):
```sql
-- WRONG (current): ltp is today's settlement = same as _ltp → chg% = 0
COALESCE(NULLIF(db.ltp, 0), NULLIF(db.close_price, 0)) AS previous_close

-- CORRECT (fix): prev_close column = BHAV at 08:00 = semantically correct
COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close
```

Update three tests in `test_positions_prev_close.py` that assert the old (wrong) pattern.

## Agents
- backend: In `backend/api/routes/positions.py` line 277, change:
  `COALESCE(NULLIF(db.ltp, 0), NULLIF(db.close_price, 0)) AS previous_close`
  to:
  `COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close`
  No other lines change. Live path (lines 946, 1027-1030) is correct and untouched.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Update three tests in `backend/tests/test_positions_prev_close.py` that assert
  `COALESCE(NULLIF(db.ltp, 0), NULLIF(db.close_price, 0)) AS previous_close` in the snapshot path:
  `TestSQLTextPatterns::test_positions_snapshot_sql_uses_coalesce` (line 88-98),
  `TestPositionsSnapshotSQLLogic::test_snapshot_sql_coalesce_column_exists` (line 411-419),
  `test_positions_snapshot_no_old_pattern_in_select` (line 469-479).
  New assertion: check for `COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close`.
  Add regression-guard: assert `COALESCE(NULLIF(db.ltp, 0), NULLIF(db.close_price, 0)) AS previous_close`
  is NOT present in `_positions_snapshot` source.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(positions): snapshot previous_close = db.previous_close (BHAV at 08:00) not db.ltp (live snapshot)

## Done when
- `positions.py` line 277 uses `COALESCE(NULLIF(db.previous_close, 0), NULLIF(db.close_price, 0)) AS previous_close`
- Three stale test assertions updated; regression-guard test added
- pytest passes with 0 failures
- `chg%` on Pulse and Legs shows correct non-zero values after deploy
