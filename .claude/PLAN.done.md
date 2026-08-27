# Plan: Fix holdings NAV undercount (71.10L→1.80C) + snapshot Day% animation + snapshot qty bug

## Context

Three bugs found via audit:

**Bug A (P1) — Holdings NAV undercount in NavStrip:**
`compute_firm_nav` → `_holdings_from_df` sums `cur_val` for ALL rows where `cur_val != 0`.
For Dhan/Groww rows where `backfill_market_data` couldn't get a quote,
`cur_val = avg × qty` (cost basis, not market value) — but it's > 0, so it passes the
filter and goes into `cv_sum` instead of `_ltp_fallback_sum`. The route rescues this
via `_override_stale_ltp_from_ticker` in `holdings.py:276`, but `compute_firm_nav`
never calls that step. Fix: in `_holdings_from_df`, detect rows with `last_price <= 0`
AND `cur_val > 0` — those are cost-basis rows masquerading as market values — and route
them through `_ltp_fallback_sum` (ticker rescue) instead of `cv_sum`.

**Bug B (P1) — Snapshot Day% shows refresh animation in closed hours:**
`pulseUnified.js:471` calls `livePositionDayPnl` with `marketOpen: true` hardcoded.
In closed hours, SSE ticks still arrive (MCX) and shift `row.day_pnl` on each
`buildUnified` call → `setGridOption('rowData', pRows)` fires → ag-Grid re-renders
Day% cells → visible flash. Fix: pass actual `isMarketOpen()` value.

**Bug C (P2) — Snapshot writer uses opening_quantity instead of quantity:**
`daily_snapshot.py:357` — `_backfill_market_data_dicts` uses `qty_col="opening_quantity"`
(should be `"quantity"` to match live path). For partially-sold holdings this inflates
snapshot pnl/cur_val.
`daily_snapshot.py:415` — `_snap_holding_eod_vals` tries `opening_quantity` first
(should prefer `quantity`, same as `_holdings_rows` at line 461).

## Task

### Fix A — nav.py `_holdings_from_df`

File: `backend/api/algo/nav.py`

Current code (lines 235-238):
```python
cv_sum = float(
    lf.filter(pl.col("_cv") != 0.0).select(pl.col("_cv").sum()).to_series()[0] or 0.0
)
ltp_sum = _ltp_fallback_sum(lf.filter(pl.col("_cv") == 0.0), ticker)
```

Problem: Dhan/Groww rows with stale LTP have `cur_val = avg×qty > 0` (cost basis),
so they land in `cv_sum` with wrong values. Ticker fallback only fires for `_cv == 0`.

Fix: Split `cv_sum` into two groups:
- Rows with valid LTP (`last_price > 0`) AND `cur_val > 0` → trust `cur_val` (already correct)
- Rows with stale/zero LTP (`last_price <= 0`) AND `cur_val > 0` → route to `_ltp_fallback_sum`
  (these are cost-basis rows masquerading as market values; ticker can rescue them)
- Rows with `cur_val == 0` → existing `_ltp_fallback_sum` path (unchanged)

Only apply `last_price` split when the column exists in the frame (guard with `if "last_price" in lf.columns`).

### Fix B — pulseUnified.js `marketOpen`

File: `frontend/src/lib/data/pulseUnified.js`

Around line 471, find the call to `livePositionDayPnl` that passes `marketOpen: true`.
Change to pass the actual market state. `isMarketOpen()` or equivalent is available
in the module — check what function/import is used elsewhere in the file for market state.
If not imported, import it from `$lib/data/marketState.js` or wherever it lives in the
codebase (search for `isMarketOpen` usage in other files in `frontend/src/lib/data/`).

### Fix C — daily_snapshot.py qty column

File: `backend/api/algo/daily_snapshot.py`

Line 357: Change `qty_col="opening_quantity"` → `qty_col="quantity"` in the
`_backfill_market_data_dicts` call.

Line 415: In `_snap_holding_eod_vals`, change qty resolution from:
`int(r.get("opening_quantity") or r.get("quantity") or 1)`
to:
`int(r.get("quantity") or r.get("opening_quantity") or 1)`

## Agents

- backend: Apply Fix A (nav.py `_holdings_from_df` stale-LTP split) and Fix C
  (daily_snapshot.py two lines: qty_col + eod_vals qty order).
  For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
  - `backend/api/algo/nav.py` change → add/update a pytest test in `backend/tests/` covering the changed lines
  - `backend/api/algo/daily_snapshot.py` change → add/update a pytest test in `backend/tests/` covering the changed lines
- frontend: Apply Fix B (pulseUnified.js `marketOpen: true` → actual market state).
  Find where `isMarketOpen` (or equivalent closed-hours check) is imported/used elsewhere
  in `frontend/src/lib/data/` and use the same pattern. Write a Vitest test in
  `frontend/src/lib/__tests__/` covering the changed logic.
  For every file you change or create, you MUST write or update at least one test. This is mandatory.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(nav): stale-LTP holdings use ticker rescue in NAV + snapshot Day% marketOpen fix + snapshot qty col

## Done when

- `_holdings_from_df` routes rows with `last_price <= 0` AND `cur_val > 0` through
  `_ltp_fallback_sum` instead of `cv_sum`
- `pulseUnified.js` passes actual market state (not hardcoded `true`) to `livePositionDayPnl`
- `daily_snapshot.py` uses `quantity` (not `opening_quantity`) in both patched places
- pytest green, svelte-check 0 errors, vitest green
