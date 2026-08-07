# Plan: Fix Dhan holdings day% — wrong day_change from missing prev_close

## Context

Dhan's `holdings()` API doesn't return `previousClosePrice`, so `_dhan_normalise_one_holding()`
sets `close_price = 0` and `day_change = ltp - 0 = ltp` (e.g. 3952 for SIEMENS).

`_backfill_market_data_dicts()` in the snapshot path fetches Kite's `ohlc.close` to patch
`close_price`. But `_backfill_recompute_derived` has a guard `not r.get("day_change")` that
prevents recomputing `day_change` when Dhan already (wrongly) set it. So the day_change
remains 3952 instead of being corrected to `ltp - prev_close`.

Additionally, `_snap_holding_eod_vals` stores raw per-share `day_change` as `day_pnl`, but
`holdings.py` divides by `close_notional = prev_close × qty` (expecting total P&L). For
qty > 1 this understates the percentage by 1/qty.

**Note on Kite's ohlc.close reset**: After NSE settlement (~3:30–18:16 IST), Kite updates
`ohlc.close` from yesterday's close to today's close. Snapshots taken AFTER this reset
(e.g. nightly 02:12) will see `close_price = ltp`. The 15:35 and 18:16 EOD snapshots
(before reset) will see the correct prior close. Fix ensures the EOD snapshot is the one
that counts; nightly overwrite is a separate concern (do not address now).

## Task

Two targeted code fixes in `backend/api/algo/daily_snapshot.py`:

1. **Bug A — `_backfill_recompute_derived` / `_snap_patch_single_price_row`**  
   Track whether `close_price` was originally ≤ 0 before patching. Pass `close_was_missing`
   flag to `_backfill_recompute_derived`. Recompute `day_change = ltp - close` unconditionally
   when `close_was_missing=True` (regardless of whether `day_change` was already set).
   This fixes Dhan's stale `day_change = ltp` value after backfill patches `close_price`.

2. **Bug B — `_snap_holding_eod_vals`**  
   Multiply `day_change × qty` so `day_pnl` stores total day P&L (not per-share). This
   aligns with how positions store `day_pnl` (via `naive_day_pnl = (ltp - cls) × qty`)
   and with the denominator `close_notional = prev_close × qty` in `holdings.py`.

## Agents

- backend: Fix `_snap_patch_single_price_row` and `_backfill_recompute_derived` in `daily_snapshot.py`:
  1. In `_snap_patch_single_price_row` (line 200): capture `close_was_missing = (_old_cls <= 0)` before patching, pass it to `_backfill_recompute_derived(r, qty_col, close_was_missing=close_was_missing)`.
  2. In `_backfill_recompute_derived` (line 231): add `close_was_missing: bool = False` param. Change line 243 guard from `not r.get("day_change")` to `(not r.get("day_change") or close_was_missing)`.
  3. In `_snap_holding_eod_vals` (line 392): change `day_pnl_v = float(day_change)` to `day_pnl_v = float(day_change) * int(r.get("opening_quantity") or r.get("quantity") or 1)`.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add/update tests in `backend/tests/test_daily_snapshot.py` and `backend/tests/test_holdings_fetch_helpers.py`:
  - Test that `_backfill_recompute_derived` recomputes `day_change` when `close_was_missing=True` even if `day_change` was already set (Dhan case).
  - Test that `_snap_holding_eod_vals` returns `day_change × qty` as `day_pnl_v` for qty > 1.
- playwright: skip

## Files to change

- `backend/api/algo/daily_snapshot.py` — lines 200–244 (two functions)
- `backend/tests/test_daily_snapshot.py` — new/updated tests

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): recompute Dhan day_change after close_price backfill; store total day_pnl for holdings

## Done when

- `_backfill_recompute_derived` recomputes `day_change` when `close_price` was originally 0 (Dhan case)
- `_snap_holding_eod_vals` returns `day_change × qty` as `day_pnl_v`
- pytest passes with new tests covering both fixes
- After next EOD snapshot (15:35 IST), SIEMENS/WAAREEENER day% shows actual daily move (~0-1%), not 20–50%
