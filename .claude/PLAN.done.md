# Plan: Fix _is_exchange_open_at holiday blindness → Dhan holdings NULL ltp on holiday startup

## Context

Today is Aug 15, 2026 (Independence Day). Operator observed Dhan account holdings not appearing
in the Holdings grid while Kite holdings are visible.

Root cause: `daily_snapshot.py:_is_exchange_open_at()` (line 44) checks time-of-day only — no
holiday awareness. When the server restarts on a holiday between 09:15–15:30 IST:

1. Startup snapshot correctly detects markets closed via `is_market_open()` (which uses the
   holiday calendar) and fires `snapshot_daily_book()`.
2. Inside `_holdings_rows`, `_is_exchange_open_at("NSE", now_ist)` returns **True** (time only,
   no holiday check) → `mid_session=True` → `ltp_val = None`.
3. UPSERT stores `ltp = NULL`. `COALESCE(EXCLUDED.ltp, daily_book.ltp)` preserves prior non-NULL
   values for Kite (Aug 14 EOD snapshot was good). Dhan accounts with no prior non-NULL row stay NULL.
4. `_HOLDINGS_SNAPSHOT_SQL` filters `ltp IS NOT NULL` → Dhan accounts excluded → not shown.

Kite is unaffected because its Aug 14 15:35 IST snapshot (`mid_session=False` since 15:35 > 15:30)
wrote non-NULL ltp, which COALESCE preserved.

## Task

Add a `market_open: bool` parameter to `snapshot_daily_book()` (and propagate to `_holdings_rows`
and `_positions_rows`) so callers that KNOW markets are closed can override `_is_exchange_open_at`.

When `market_open=False`, skip the `_is_exchange_open_at` call and treat `mid_session=False`
unconditionally — holidays and weekends then correctly capture ltp instead of emitting NULL.

## Agents

- backend: In `backend/api/algo/daily_snapshot.py`:
  1. Add `market_open: bool = True` param to `snapshot_daily_book()`, `_holdings_rows()`, `_positions_rows()`.
  2. In `_holdings_rows` and `_positions_rows`, change:
     `mid_session = _is_exchange_open_at(exchange, now_ist)`
     to:
     `mid_session = market_open and _is_exchange_open_at(exchange, now_ist)`
  3. In `snapshot_daily_book()`, pass `market_open` down to the two row builders.
  4. In `background.py:_task_daily_snapshot` (and the startup path at line ~1853):
     - For the startup snapshot (fires when `not (_nse_open or _mcx_open)`): pass `market_open=False`
     - For the 15:35 settlement snapshot: derive `market_open` from the probe result and pass it
     - For `POST /api/admin/pnl/snapshot` endpoint: compute `market_open` from `is_market_open()` and pass it
  5. Do NOT change `_is_exchange_open_at` itself — it's correct for the mid-session guard; only
     the override path bypasses it.

- backend-test: Add/update tests in `backend/tests/test_daily_snapshot.py`:
  - Test that `_holdings_rows` with `market_open=False` sets `mid_session=False` even at 10:00 IST
  - Test that `_holdings_rows` with `market_open=True` (default) still sets `mid_session=True` at 10:00 IST
  - Test that `snapshot_daily_book(market_open=False)` produces non-NULL ltp for holdings during
    normal-session time on a holiday

- broker: skip
- frontend: skip
- doc: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(snapshot): pass market_open flag to row builders so holiday startup captures non-NULL ltp for Dhan holdings

## Done when

- `_holdings_rows` and `_positions_rows` accept `market_open` param
- Startup snapshot passes `market_open=False` when `not (_nse_open or _mcx_open)`
- Settlement snapshot passes `market_open=(nse_open or mcx_open)` 
- pytest passes with new tests covering the holiday path
- Manually triggering `POST /api/admin/pnl/snapshot` should now write non-NULL ltp for all accounts
  regardless of time of day
