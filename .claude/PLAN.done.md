# Plan: Fix holdings day P&L — expose previous_close through API and use it in frontend

## Context

Holdings day P&L is wrong because:

1. **Backend** `_override_stale_close_for_holdings` uses `daily_book.ltp` (pre-8AM query) to patch `close_price`, but `ltp` can drift post-settlement. The stable reference is `daily_book.previous_close` (frozen COALESCE — never overwritten once set). Also, the function only writes `previous_close` to *patched* rows, not all rows — so the field is absent from most rows.

2. **API response** omits `previous_close` entirely — `HoldingRow` schema and `_ROW_COLS` in `holdings.py` don't include it — so the frontend has no reliable frozen close available.

3. **Frontend** `holdingsDayPnlStore.svelte.js` (line 73) and `pulseUnified.js` (line 555) both use `close_price` only, with no `previous_close` fallback. When Kite's `close_price` drifts at settlement, day P&L zeros out incorrectly.

4. **Missing backstop**: `apply_day_change_backstop()` is called for positions but NOT for holdings. Holdings with `day_change_val=0` and nonzero `pnl` have no recovery path.

Root-cause sequence: Kite sets `close_price = ltp = settlement_price` at session close → `|ltp − close_price| ≤ 0.005` guard fires → frontend falls back to `day_change_val` → if `day_change_val` is 0 or stale, H slot shows 0.

---

## Task

1. **Backend — `_override_stale_close_for_holdings`** (`backend/api/routes/holdings.py` ~line 318):
   - Change DB query from `daily_book.ltp` to `COALESCE(daily_book.previous_close, daily_book.ltp)` as the reference close
   - Write `previous_close` column to the raw DataFrame for **ALL** rows (not just patched ones); use the queried COALESCE value directly

2. **Backend — `_ROW_COLS`** (`backend/api/routes/holdings.py` ~line 252):
   - Add `'previous_close'` to the column list so it survives Polars row-build

3. **Backend — `HoldingRow`** (`backend/api/schemas.py` ~line 15):
   - Add `previous_close: float = 0.0` field

4. **Frontend — `holdingsDayPnlStore.svelte.js`** (line 73):
   - Change `const closePx = Number(h?.close_price) || 0;`
   - To `const closePx = Number(h?.previous_close) || Number(h?.close_price) || 0;`

5. **Frontend — `pulseUnified.js`** (line 555):
   - Change `const holdClose = Number(r.close_price) || 0;`
   - To `const holdClose = Number(r.previous_close) || Number(r.close_price) || 0;`

6. **Backend — apply backstop for holdings** (`backend/api/routes/holdings.py` in `_fetch()`):
   - After row-building, call `apply_day_change_backstop()` on the holdings DataFrame (same as positions.py:574)
   - Import `apply_day_change_backstop` from `backend.api.algo.pnl_math` if not already imported

---

## Agents

- backend: In `backend/api/routes/holdings.py`: (1) update `_override_stale_close_for_holdings` to query `COALESCE(daily_book.previous_close, daily_book.ltp)` and write `previous_close` column to ALL raw DataFrame rows; (2) add `'previous_close'` to `_ROW_COLS`; (3) in `_fetch()` call `apply_day_change_backstop()` on built rows after row construction (import from `backend.api.algo.pnl_math`). In `backend/api/schemas.py`: add `previous_close: float = 0.0` to `HoldingRow`. Do NOT change `_build_holding_row_from_snapshot` — snapshot path already handles `previous_close` correctly.
- frontend: In `frontend/src/lib/data/holdingsDayPnlStore.svelte.js` line 73, change `close_price` to prefer `previous_close` first: `const closePx = Number(h?.previous_close) || Number(h?.close_price) || 0;`. In `frontend/src/lib/data/pulseUnified.js` line 555, same change: `const holdClose = Number(r.previous_close) || Number(r.close_price) || 0;`.
- broker: skip
- doc: skip
- backend-test: Add tests in `backend/tests/test_holdings_previous_close.py`: (1) verify `previous_close` field present in live-path API response; (2) verify `_override_stale_close_for_holdings` writes `previous_close` for all rows using COALESCE(previous_close, ltp); (3) verify `apply_day_change_backstop` recovers holdings with `day_change_val=0, pnl≠0`.
- playwright: skip

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

---

## Commit message

fix(holdings): expose previous_close via API + use it as day P&L reference in frontend

Holdings day P&L was zeroing at/after settlement because Kite sets close_price=ltp=settlement,
triggering the |ltp−close|≤0.005 guard → falls back to day_change_val (often 0).

Fix: query COALESCE(previous_close, ltp) from daily_book (frozen field, never overwritten),
write it to all holding rows, expose in HoldingRow schema, and prefer it over close_price
in holdingsDayPnlStore + pulseUnified. Also add apply_day_change_backstop for holdings.

---

## Done when

- `HoldingRow` API response includes `previous_close` field (non-zero for any holding with a prior-session snapshot in daily_book)
- `holdingsDayPnlStore` uses `previous_close` as reference close; H slot shows correct day P&L after NSE settlement
- `apply_day_change_backstop` called for holdings; recovers `day_change_val=0` edge cases
- pytest green, svelte-check 0 errors
