# Plan: Fix positions + holdings day P&L zero after NSE settlement

## Context

NavStrip P slot 1 (today's positions day P&L) and holdings day P&L both show ₹0
from ~16:00 IST until next session open. Same root cause, different manifestation:

**Positions** — `_positions_snapshot()` `prev_batch` CTE has no date boundary.
After today's 15:30 and 16:15 snapshots land in daily_book, `prev_batch` picks
today's 15:30 row as `prev_ltp` (≈ today's OCP). `close_price ≈ last_price` →
`day_change_val = 0` → `pnl − prev_settlement_pnl ≈ 0` → P = 0.

**Holdings** — `_holdings_snapshot()` uses `MAX(captured_at)` (correct: picks 16:15
batch). `previous_close` in daily_book is frozen write-once (correct: yesterday's
settlement). BUT `day_pnl` is overwritten on every UPSERT. The 16:15 UPSERT
computes `day_pnl = broker.pnl − (kite_close − avg) × qty`. After settlement
Kite sets `close_price = today's OCP`, so `day_pnl → 0`. The frozen
`previous_close` is correct but the snapshot reader uses the stored `day_pnl`
instead of recomputing from `previous_close`.

Both fixes are the same principle: **use yesterday's EOD LTP as the session
baseline, not today's post-settlement price from Kite.**

## Task

**Fix 1 — positions** (`positions.py`): Add `AND db.captured_at < :today_ist_midnight`
(+ `AND db.ltp IS NOT NULL AND db.ltp > 0`) to `prev_batch` CTE so it always
resolves to yesterday's EOD row.

**Fix 2 — holdings** (`holdings.py`): In `_build_holding_row_from_snapshot()`,
recompute `day_change_val` from the frozen `previous_close` instead of trusting
the stored `day_pnl` which is overwritten with 0 at 16:15:
```python
# Replace:
day_change_val = day_pnl_f
# With:
day_change_val = (ltp_f - previous_close_f) * qty_i if previous_close_f > 0 else day_pnl_f
```
Also recompute `day_change_percentage` using the same `previous_close_f` denominator
(it already does this correctly — just ensure it uses the recomputed `day_change_val`).

## Agents

- backend: Apply both fixes:

  **Fix 1 — `backend/api/routes/positions.py`**, `_positions_snapshot()` (lines 41–186):
  1. Compute `today_ist_midnight` before the SQL call (same pattern as lines 719–721):
     ```python
     from backend.shared.helpers.date_time_utils import timestamp_indian
     today_ist_midnight = timestamp_indian().replace(hour=0, minute=0, second=0, microsecond=0)
     ```
  2. In `prev_batch` CTE (lines 82–91) add:
     ```sql
     AND db.ltp IS NOT NULL AND db.ltp > 0
     AND db.captured_at < :today_ist_midnight
     ```
  3. Add `today_ist_midnight=today_ist_midnight` to the existing `.bindparams(today_ist=_today_ist)` call.

  **Fix 2 — `backend/api/routes/holdings.py`**, `_build_holding_row_from_snapshot()` (lines 84–137):
  Replace `day_change_val = day_pnl_f` with:
  ```python
  day_change_val = (ltp_f - previous_close_f) * qty_i if previous_close_f > 0 else day_pnl_f
  ```
  `day_change_percentage` already uses `previous_close_f` as denominator — no change needed there,
  but update its numerator reference from `day_pnl_f` to `day_change_val`.

  **Fix 3 — `backend/api/routes/positions.py`**, `_positions_snapshot()` row mapping (lines 127–177):
  After the `prev_batch` fix gives correct `prev_ltp` (yesterday's EOD), also recompute
  `day_change_val` in the snapshot reader so each **grid row** shows the correct day delta
  (not the stored `day_pnl` which is 0 after Kite's settlement update):
  ```python
  # Add after computing prev_close_val and before build_snapshot_position_row:
  stored_day_pnl = ...  # existing day_pnl from SQL
  # Recompute from prev_close when available; fall back to stored value
  computed_day_pnl = (
      (float(ltp) - float(prev_close_val)) * effective_qty
      if prev_close_val and float(prev_close_val) > 0 and ltp
      else stored_day_pnl
  )
  ```
  Pass `computed_day_pnl` instead of `day_pnl` to `build_snapshot_position_row`.
  This fixes the individual row `day_change_val` displayed in the Pulse page grid,
  not just the NavStrip aggregate (which goes via `baseDayPnlForPosition`).

- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add tests:

  In `backend/tests/test_positions_route.py` (lines 552–625):
  1. `test_prev_batch_excludes_todays_snapshots_uses_yesterday_ltp` — daily_book has
     15:30 row (today), 16:15 row (today), and one yesterday row; assert snapshot
     returns `close_price = yesterday_ltp`, not today's.
  2. `test_prev_batch_null_ltp_rows_excluded` — only prior row has `ltp=NULL`;
     assert `close_price` falls back to `previous_close`.

  In `backend/tests/test_closed_hours_snapshot_routes.py` (after line 979):
  3. `test_holdings_snapshot_day_change_val_uses_previous_close_not_stored_day_pnl` —
     daily_book row has `day_pnl=0.0` (simulating post-settlement clobber) but
     `previous_close=98.0` and `ltp=100.0`, `qty=10`; assert snapshot row returns
     `day_change_val = 20.0` (= (100−98)×10), not 0.
  4. `test_positions_snapshot_day_change_val_recomputed_from_prev_close` —
     daily_book has yesterday row (`ltp=98`) and today 16:15 row (`ltp=100, day_pnl=0`);
     assert snapshot row `day_change_val = (100−98)×qty`, not 0.

- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(positions,holdings): use yesterday EOD as day P&L baseline after NSE settlement

Positions `_positions_snapshot` `prev_batch` CTE had no date boundary — after
15:30+16:15 snapshots landed, it picked today's 15:30 as `prev_ltp`, making
`close_price ≈ last_price` and collapsing P&L to ₹0. Holdings `day_pnl`
overwritten to 0 at 16:15 because Kite flips `close_price` to today's OCP
post-settlement. Both snapshot readers now recompute `day_change_val` from
yesterday's frozen EOD LTP so NavStrip P slot and Pulse page grid rows all
show correct day P&L after settlement.

## Done when

`pytest backend/tests/test_positions_route.py backend/tests/test_closed_hours_snapshot_routes.py`
passes. After NSE settlement:
- Each positions grid row shows correct `day_change_val = (ltp − yesterday_ltp) × qty`
- Each holdings grid row shows correct `day_change_val = (ltp − previous_close) × qty`
- NavStrip P slot shows correct aggregate (via `baseDayPnlForPosition` path 1)

---

## Key files

- `backend/api/routes/positions.py` lines 41–186 (`_positions_snapshot` SQL + mapping)
- `backend/api/routes/positions.py` lines 684–792 (`_override_stale_close_from_snapshot` — `today_ist_midnight` pattern to reuse)
- `backend/api/routes/holdings.py` lines 84–137 (`_build_holding_row_from_snapshot`)
- `backend/tests/test_positions_route.py` lines 552–625 (extend with 2 new tests)
- `backend/tests/test_closed_hours_snapshot_routes.py` lines 919–979 (extend with 1 new test)

## Exact SQL diff — positions `prev_batch` CTE

```sql
-- BEFORE (lines 82–91)
prev_batch AS (
    SELECT DISTINCT ON (db.account, db.symbol)
        db.account, db.symbol,
        db.ltp       AS prev_ltp,
        db.total_pnl AS prev_settlement_pnl
    FROM daily_book db
    JOIN latest_batch lb ON db.account = lb.account
    WHERE db.kind = 'positions'
      AND db.total_pnl IS NOT NULL
      AND db.captured_at < lb.max_at
      AND db.captured_at >= lb.max_at - INTERVAL '2 days'
    ORDER BY db.account, db.symbol, db.captured_at DESC
)

-- AFTER
prev_batch AS (
    SELECT DISTINCT ON (db.account, db.symbol)
        db.account, db.symbol,
        db.ltp       AS prev_ltp,
        db.total_pnl AS prev_settlement_pnl
    FROM daily_book db
    JOIN latest_batch lb ON db.account = lb.account
    WHERE db.kind = 'positions'
      AND db.total_pnl IS NOT NULL
      AND db.ltp IS NOT NULL AND db.ltp > 0
      AND db.captured_at < lb.max_at
      AND db.captured_at < :today_ist_midnight
      AND db.captured_at >= lb.max_at - INTERVAL '2 days'
    ORDER BY db.account, db.symbol, db.captured_at DESC
)
```
