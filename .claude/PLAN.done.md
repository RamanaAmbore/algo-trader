# Plan: Fix 9 defects — Dhan, LTP flash sync, NavStrip SSOT, lot-size, conn log

## Task

Nine confirmed bugs across broker and frontend layers. Template orders fix deferred per operator.
The offset-position grouping (close + cancel template chain as one unit) is agreed in principle
but is a separate plan — requires a new data model (group_id), close-path extension, and UI grouping.

**Broker layer (dhan.py / broker_apis.py):**
1. **Dhan holdings = 0**: `_unwrap` returns `[]` when dhanhq v2.2.0 error response has `data=""` (not a list); never detected as a failure.
2. **Dhan chip wrong status**: `_record_fetch(ok=True)` fires even when rows=[] → `last_ok_at` updates → chip stays green when data is bad.
3. **Connection log missing success**: `_record_fetch` emits `fetch_ok_recovery` only when recovering from a prior failure. Normal healthy fetches emit nothing → log is silent when broker connects successfully. Fix: emit `fetch_ok` event on every healthy (non-recovery) successful fetch.

**Frontend layer:**
4. **LTP flash not showing + pinned/other grid desync across all pages**: `is_animating` is correctly `True` for live rows (set by `resolve_current_price` when exchange is open). Root cause is purely timing: MarketPulse tickBus subscriber calls `refreshCells` via a 250ms-throttled idle path (up to 750ms delay), while flash clearance fires at 300ms → flash already gone when grid updates. Pinned grid gets immediate `refreshCells` in tickBus (correct); all other grids don't → visible desync. Fix: add immediate `refreshCells` for ALL non-pinned grids (positions, holdings, win, lose in MarketPulse; audit other pages with LTP grids).
5. **NavStrip H slot out of sync with MarketPulse holdings TOTAL**: `_accumTotalsRow` (~line 1952 of MarketPulse.svelte) uses `r._broker_pnl` (frozen broker snapshot) for the TOTAL row while individual rows show live-LTP-recomputed `r.pnl`. TOTAL doesn't update with ticks.
6. **NavStrip / Pulse / Dashboard not on same SSOT for positions + holdings**: NavStrip and Dashboard share `positionsStore` / `holdingsStore` (key='md.positions'). MarketPulse uses separate `pulsePositionsStore` / `pulseHoldingsStore` (key='md.pulse.positions') — independent cached copies, up to 30s lag between pages. Margins + cash already use shared `fundsStore` (no change needed). Fix: remove `pulsePositionsStore` / `pulseHoldingsStore`; make MarketPulse use `positionsStore` / `holdingsStore` directly. Verify `MarketDataStore.load()` deduplicates concurrent calls so removing the isolation is safe.
7. **CRUDEOIL add-buy lot size resets to 1**: `orders/+page.svelte` `place-order` branch (~line 567–573) doesn't pass `currentQty` to SymbolPanel; OrderTicket's lot-size `$effect` guards on `currentQty === 0` → `_lots` stays at 1. Close-buy branch correctly passes `currentQty={_ctxQty}`.
8. **Cost shows wrong qty**: same root cause as #7 — `_qty = 1 × lotSize` instead of actual position qty.
9. **NavStrip H slot reads raw holdingsStore (not live-recomputed)**: `PositionStrip._liveHoldingsTotal` (~line 452) = Σ holdingsStore `h.pnl` (broker snapshot). After SSOT fix (#6), both Pulse and NavStrip read the same store. Item #5 fix makes `_accumTotalsRow` use live-recomputed pnl; NavStrip H slot should derive its value the same way.

## Agents

- broker: Fix items 1+2+3.
  - `backend/brokers/adapters/dhan.py` `_unwrap` (~line 1528): detect `resp["data"]` is not a list (error shape `data=""`) and raise a distinct named error instead of returning `[]`.
  - `backend/brokers/adapters/dhan.py` `holdings()` (~line 1009): check `_safe_call` return for `{"status":"failure"}` shape before calling `_normalise_holdings`; call `_record_fetch(account, ok=False, ...)` on failure shape.
  - `backend/brokers/broker_apis.py` `_fetch_holdings_local` (~line 1404): gate `_record_fetch(account, ok=True)` to only fire when `rows` is non-empty.
  - `backend/brokers/broker_apis.py` `_record_fetch` non-recovery `ok=True` path (~line 1048): emit `"fetch_ok"` event on every healthy (non-recovering) success.
  - `frontend/src/lib/LogPanel.svelte` (~line 417): add `fetch_ok` to the green event_type list alongside `fetch_ok_recovery`.
  - For every file changed, write or update a test in `backend/tests/broker/` covering the changed lines.

- backend: skip

- frontend: Fix items 4+5+6+7+8+9.
  Files: `MarketPulse.svelte`, `PositionStrip.svelte`, `marketDataStores.svelte.js`, `orders/+page.svelte`.
  - (4) MarketPulse tickBus subscriber (~line 1542–1558): add immediate `refreshCells` for `gridPositions`, `gridHoldings`, `gridWin`, `gridLose` right after setting flash state — same pattern as PerformancePage.svelte (~line 1197–1221). Also grep for other pages with LTP grids and apply same pattern.
  - (5) MarketPulse `_accumTotalsRow` (~line 1952): change `const rowPnl = (r._broker_pnl != null) ? r._broker_pnl : r.pnl` to use `r.pnl` (live-recomputed) instead of `r._broker_pnl`.
  - (6) SSOT: remove `pulsePositionsStore` / `pulseHoldingsStore` from `marketDataStores.svelte.js`; update MarketPulse imports to use `positionsStore` / `holdingsStore`; verify `MarketDataStore.load()` has dedup/coalesce so concurrent calls from multiple subscribers don't race.
  - (7+8) `orders/+page.svelte` place-order SymbolPanel block (~line 567–573): add `currentQty={_ctxQty}` prop.
  - (9) `PositionStrip._liveHoldingsTotal` (~line 452): after SSOT unification, derive the H-slot value from the same live-recomputed `baseDayPnlForPosition` path used by MarketPulse (not raw `h.pnl`).
  - For every file changed, write or update a Playwright spec in `frontend/tests/` covering the changed flow.

- backend-test: Add pytest tests:
  - (a) `test_dhan_holdings_error_shape` — mock `_safe_call` returning `{"status":"failure","data":"","remarks":{}}` → assert returns `[]` AND `_record_fetch(ok=False)` called.
  - (b) `test_fetch_holdings_local_empty_rows` — mock broker returning `[]` → assert `_record_fetch(ok=False)` (not ok=True).
  - (c) `test_record_fetch_emits_fetch_ok` — mock normal non-recovering success → assert `"fetch_ok"` event emitted.
  Tests in `backend/tests/broker/` for a+b+c.

- playwright: skip
- doc: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(multi): Dhan holdings/chip/log, LTP flash sync, NavStrip SSOT, lot-size

## Done when
- Dhan holdings returns rows OR logs ok=False (never silently empty on API error shape)
- Dhan chip shows amber/red when holdings returns error/empty
- Connection log shows green `fetch_ok` on every healthy broker fetch (not only recovery)
- LTP flash appears immediately and in sync on ALL grids across all pages — pinned, positions, holdings, win, lose flash at the same time on the same tick
- MarketPulse holdings TOTAL row updates with live-LTP-recomputed pnl (not frozen broker snapshot)
- NavStrip, MarketPulse, and Dashboard all read from the same `positionsStore` / `holdingsStore` (single SSOT)
- NavStrip H slot pnl matches MarketPulse holdings TOTAL live-recomputed pnl
- CRUDEOIL add-buy opens ticket with correct lot size (not 1); cost shows correct qty
- All pytest pass, svelte-check 0 errors
