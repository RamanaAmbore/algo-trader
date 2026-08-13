# Plan: Fix MCX post-close ΔP=0 + guard UPSERT + footer co-founder

## Task

**Bug**: After MCX closes at 23:30 IST, positions day P&L (ΔP) shows 0 until 00:15.

Root cause: The daily_book writer fires NSE settlement at 16:15 and MCX settlement at 00:15.
At 16:15, MCX positions have `mid_session=True` (MCX is still open) →
`_snap_position_eod_vals` returns `ltp=None, day_pnl=None`. UPSERT writes NULL.
Between 23:30 (MCX close) and 00:15 (MCX settlement), the snapshot serves `day_pnl=NULL→0`.

Holdings has the same per-exchange mid_session logic but doesn't suffer because
NSE closes at 15:30 and the 16:15 NSE settlement snapshot fires when NSE mid_session=False.
MCX has no equivalent EOD snapshot — that's the gap.

**Fix 1 (primary)**: Add MCX-close snapshot at 23:31 IST in `_task_daily_snapshot`. At
23:31 MCX is just closed → mid_session=False → `_snap_compute_day_pnl(ltp=settlement,
cls=prior_close, qty)` gives correct non-zero day_pnl. Exactly mirrors the NSE 16:15 pattern.

**Fix 2 (guard)**: COALESCE-NULLIF on `day_pnl` in `_UPSERT_SQL`. Prevents the 00:15
settlement snapshot (or any future mid-session NULL write) from overwriting the 23:31
correct value. Same rationale as the existing `previous_close` COALESCE freeze.

**Fix 3 (footer)**: Add "Gopi Podicheti" alongside "Ramana R. Ambore" in the app footer.

## Agents

- backend: In `backend/api/background.py`, inside `_task_daily_snapshot`, add an MCX-close
  snapshot trigger that fires between 23:31 and 23:40 IST (9-minute window), once per
  trade-date. Mirror the existing MCX settlement pattern:
  ```python
  _MCX_CLOSE_H, _MCX_CLOSE_M = 23, 31
  _mcx_close_done: Optional[date] = None
  ...
  if dtime(_MCX_CLOSE_H, _MCX_CLOSE_M) <= now.time() < dtime(23, 40):
      if _mcx_close_done != today:
          logger.info("Background: 23:31 IST — firing MCX close snapshot")
          await _fire_snapshot("mcx-close")
          _mcx_close_done = today
  ```
  Place this block BEFORE the MCX settlement check (00:15) in the main poll loop.

  In `backend/api/algo/daily_snapshot.py` at `_UPSERT_SQL` (line 681), change:
  ```sql
  day_pnl = EXCLUDED.day_pnl,
  ```
  to:
  ```sql
  day_pnl = COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl),
  ```
  This mirrors the `previous_close = COALESCE(daily_book.previous_close, EXCLUDED.previous_close)`
  pattern already in the same UPSERT.

- frontend: In the app footer (find via grep for "Ramana" in frontend/src/), add
  "Gopi Podicheti" as a co-founder alongside "Ramana R. Ambore". Match existing font/style.

- broker: skip
- doc: skip
- backend-test: Add `backend/tests/test_daily_snapshot_mcx_close.py` with:
  1. UPSERT preserves non-zero day_pnl when subsequent write sends day_pnl=0 (NULLIF guard)
  2. UPSERT preserves non-zero day_pnl when subsequent write sends day_pnl=NULL (mid_session guard)
  3. UPSERT updates correctly when new non-zero day_pnl arrives (normal path)
  Use `_UPSERT_SQL` imported from `backend.api.algo.daily_snapshot` with real async DB session.
  Rollback after each test.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(snapshot): MCX-close snapshot at 23:31 + COALESCE-NULLIF day_pnl guard + footer co-founder

## Done when
- `_task_daily_snapshot` fires "mcx-close" snapshot between 23:31–23:40 IST daily
- `_UPSERT_SQL` line 681 uses COALESCE(NULLIF(EXCLUDED.day_pnl, 0), daily_book.day_pnl)
- MCX positions show correct non-zero ΔP from 23:31 onward (not just from 00:15)
- "Gopi Podicheti" appears in the app footer alongside "Ramana R. Ambore"
- Pytest passes with UPSERT guard tests green
