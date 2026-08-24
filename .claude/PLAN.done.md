# Plan: pnl_per_share + snapshot path previous_close fix + positions P&L verdict

## Context

Three follow-on items from the holdings day P&L fix (commit 39c21cca):

1. **Snapshot path missing `previous_close`** — `_build_holding_row_from_snapshot` computes `previous_close_f` from daily_book but never passes it to the `HoldingRow` constructor. After market close the API returns `previous_close=0.0` for all rows; the frontend falls back to `close_price` (which happens to equal `previous_close_f` in the snapshot path, so values are still correct), but the field should be populated consistently so the frontend's priority chain (`previous_close || close_price`) is unambiguous.

2. **pnl_per_share** — new computed field: lifetime P&L per held share (`pnl / quantity`). No DB migration needed; derived on the fly. Expose from the API and add a column to every surface where holdings rows are displayed (Pulse holdings grid; PerformancePage holdings grid if applicable).

3. **Positions P&L bug verdict** — `_override_stale_close_from_snapshot` in `positions.py` uses "patched_idx only" recompute (unlike the widened holdings fix). This is intentional and correct for positions:
   - If epsilon passes (close_price ≈ daily_book.ltp), the broker's decomposed `day_change_val` is already approximately correct — no recompute needed.
   - If epsilon fails, close_price is patched and dcv is recomputed via `_compute_day_change_val`.
   - Backstop Case 2 (`oq≠0, dcv=0, pnl≠0, close>0, avg>0`) handles the remaining edge cases correctly and is valid for positions (unlike holdings which had no `overnight_quantity`).
   - **Conclusion: no equivalent bug in positions.** The backstop IS appropriate for positions.

## Task

Three changes:

**A. Fix snapshot path**: Add `previous_close=previous_close_f` to the `HoldingRow` constructor in `_build_holding_row_from_snapshot`. One line.

**B. pnl_per_share**: Compute and expose in both the live path and snapshot path. Add to schema, `_ROW_COLS`, and frontend grid column defs.

**C. No positions code change needed** (verdict only). Add a test asserting the positions backstop correctly fires Case 2 and does NOT corrupt day P&L for flat stocks.

## Agents

- backend: skip
- frontend: Add `pnl_per_share` column to Pulse holdings grid (`pulseColumns.js` `mkRightColDefs` + `rightColDefs` for holdings). Field name: `pnl_per_share`. Header: "P&L/sh". Formatter: `aggFmtGrid()` (same as `pnl`). Place after `pnl_pct` column. Also add to `mergeHoldingRows` in `pulseUnified.js`: `row.pnl_per_share = (liveHold != null && heldQty !== 0) ? (liveHold - holdAvg) : (Number(r.pnl_per_share) || 0)`. Note: frontend must receive `pnl_per_share` from the API response (backend change is in broker agent).
- broker: Three changes in `backend/brokers/broker_apis.py` and `backend/api/routes/holdings.py` and `backend/api/schemas.py`:
  1. `_enrich_holdings` in `broker_apis.py` (~line 1718-1730): After computing `pnl` in Pass 1, add `pnl_per_share = pnl / quantity` (guard: quantity ≠ 0). Polars expression: `(pl.col('pnl') / pl.col('quantity').replace(0, None)).fill_null(0.0).alias('pnl_per_share')`.
  2. `_build_holding_row_from_snapshot` in `holdings.py` (line 148-167): Add `previous_close=previous_close_f` to HoldingRow constructor. Also add `pnl_per_share=total_pnl_f / qty_i if qty_i != 0 else 0.0`.
  3. `HoldingRow` in `schemas.py` (after `pnl_percentage`): Add `pnl_per_share: float = 0.0`.
  4. `_ROW_COLS` in `holdings.py` (line 252-267): Add `'pnl_per_share'` to the list.
  5. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional. `backend/brokers/` change → add/update a pytest test in `backend/tests/broker/` covering the changed lines. No change ships without a corresponding test update.
- doc: skip
- backend-test: Add a test in `backend/tests/broker/` covering: (a) `pnl_per_share = pnl / quantity` in `_enrich_holdings` for a basic holding; (b) `_build_holding_row_from_snapshot` includes `previous_close` non-zero; (c) positions backstop Case 2 fires correctly for overnight position with stale close and does NOT corrupt dcv for a flat stock.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(holdings): snapshot previous_close + pnl_per_share field + positions backstop verdict

## Done when
- `HoldingRow` has `pnl_per_share` and `previous_close` populated in both live and snapshot paths
- Pulse holdings grid shows a P&L/sh column
- `_build_holding_row_from_snapshot` passes `previous_close=previous_close_f` to HoldingRow
- Tests green; svelte-check 0 errors

## Critical files
- `backend/api/routes/holdings.py` — `_build_holding_row_from_snapshot` (line 148-167), `_ROW_COLS` (line 252-267)
- `backend/api/schemas.py` — `HoldingRow` struct (line 15-77)
- `backend/brokers/broker_apis.py` — `_enrich_holdings` (line ~1661-1738)
- `frontend/src/lib/data/pulseColumns.js` — `mkRightColDefs` / holdings colDefs (line ~464-557)
- `frontend/src/lib/data/pulseUnified.js` — `mergeHoldingRows` (line 509-582)
