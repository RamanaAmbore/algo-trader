# Plan: Fix positions day P&L after market hours + Holdings NavStrip SSOT

## Context
Two SSOT bugs observed after MCX market close:

1. **Holdings NavStrip H:1 = 0**: `pulseHoldingsStore` is NOT included in `_tickBookPollers()`. After market close, `marketAwareInterval` in PositionStrip.svelte pauses `_load()`, so `pulseHoldingsStore` goes stale. The book poller loads `holdingsStore` but NOT `pulseHoldingsStore`. NavStrip reads `pulseHoldingsStore.value` for `_liveHoldingsToday` → shows 0.

2. **Positions day P&L = 0 after market hours**: `build_row_from_snapshot_raw` in `positions_helpers.py` resolves `prev_close_val` with wrong priority — `prev_ltp` first (the most recent batch LTP, which during a live session ≈ current LTP → day_change ≈ 0). `previous_close` (official Kite settlement, frozen on first UPSERT via COALESCE) is the correct reference but is currently the fallback only. DATA_LIFECYCLE.md §4.4 line 184 documents this wrong priority.

Holdings work correctly because their snapshot uses `previous_close` as primary (written once and frozen, so it holds yesterday's settlement price throughout the session).

## Task

**Fix 1 — Frontend** (`frontend/src/lib/data/marketDataStores.svelte.js`):
Add `pulseHoldingsStore.load()` to `_tickBookPollers()` (the `Promise.allSettled` array, around line 718). This ensures NavStrip H:1 slot (`dispHoldingsToday` from `_liveHoldingsToday` computed over `pulseHoldingsStore.value`) stays in sync with Pulse Holdings TOTAL after market close.

```javascript
// In _tickBookPollers():
await Promise.allSettled([
  positionsStore.load(),
  holdingsStore.load(),
  pulseHoldingsStore.load(),   // ← ADD THIS
  fundsStore.load(),
]);
```

**Fix 2 — Backend** (`backend/api/routes/positions_helpers.py`):
In `build_row_from_snapshot_raw` (~line 298), swap `prev_close_val` priority to prefer `previous_close` (official settlement, frozen by COALESCE on first UPSERT) over `prev_ltp` (recent batch LTP, which ≈ current LTP during a session). Also pass `actual_previous_close` (not `prev_ltp`) as the `previous_close` kwarg to `build_snapshot_position_row`.

```python
# NEW priority: official settlement first, recent batch LTP as fallback only
actual_previous_close = (
    float(previous_close) if previous_close and float(previous_close) > 0 else None
)
prev_close_val = (
    actual_previous_close
    or (float(prev_ltp) if prev_ltp and float(prev_ltp) > 0 else None)
)
computed_day_pnl = (
    (float(ltp) - actual_previous_close) * effective_qty
    if actual_previous_close and ltp
    else day_pnl
)
return build_snapshot_position_row(
    ..., computed_day_pnl, ...,
    previous_close=actual_previous_close,   # real settlement, not prev_ltp
    ...
)
```

## Agents
- backend: Fix `build_row_from_snapshot_raw` in `backend/api/routes/positions_helpers.py` around line 298. Swap `prev_close_val` priority: (a) `actual_previous_close = float(previous_close) if previous_close and float(previous_close) > 0 else None`; (b) `prev_close_val = actual_previous_close or (float(prev_ltp) if prev_ltp and float(prev_ltp) > 0 else None)`; (c) `computed_day_pnl = (float(ltp) - actual_previous_close) * effective_qty if actual_previous_close and ltp else day_pnl`; (d) pass `previous_close=actual_previous_close` to `build_snapshot_position_row`. Add a pytest test in `backend/tests/` that calls `build_row_from_snapshot_raw` directly with a simulated multi-batch scenario where `prev_ltp` equals current `ltp` (so day_pnl would be ~0 with old code) and `previous_close` equals yesterday's settlement — assert `day_change_val` is non-zero (reflects the correct overnight move). For every file you change or create, you MUST write or update at least one test covering the changed behaviour.
- frontend: Fix `_tickBookPollers()` in `frontend/src/lib/data/marketDataStores.svelte.js` — add `pulseHoldingsStore.load()` to the `Promise.allSettled([...])` array so `pulseHoldingsStore` is refreshed on every book-poller tick (every 5s live, 30min closed). This keeps NavStrip H:1 slot in sync with Pulse Holdings TOTAL after market close. For every file you change or create, you MUST write or update at least one test covering the changed behaviour.
- broker: skip
- doc: Update three documentation surfaces after the backend/frontend fixes are committed:
  1. `docs/guides/DATA_LIFECYCLE.md` §4.4 "Row reconstruction" — fix line 184 close_price resolution order: change "prev_ltp (if > 0) → previous_close (if > 0) → ltp" to "previous_close (if > 0) → prev_ltp (if > 0) → ltp". Update lines 185–187 to reflect that computed_day_pnl now uses actual_previous_close (official settlement) when available. Add a one-line note that this fixes positions showing 0 day P&L during closed hours when daily_book had multiple intraday captures. Add a changelog entry.
  2. `docs/specs/PULSE_SPEC.md` — update the positions closed-hours section (around line 151) to document the corrected priority: `previous_close` (frozen settlement) takes precedence over `prev_ltp` (recent batch). Correct any reference to `(ltp − prev_ltp)` for positions that is wrong post-fix. Add an SSOT note: positions TOTAL day P&L in Pulse and NavStrip P:1 converge via `positionsStore` (book-poller loaded). Update NAVSTRIP_SPEC.md §H:1 (around line 152–153) — add a note that `pulseHoldingsStore` is now included in `_tickBookPollers()` so H:1 (`dispHoldingsToday`) stays in sync with Pulse Holdings TOTAL during closed hours; the store is no longer dependent on `marketAwareInterval` alone.
- backend-test: skip (backend agent handles tests inline)
- playwright: Write two Playwright specs in `frontend/e2e/`:
  1. `pnl_positions_closed_hours_ssot.spec.js` — Verifies that after market close, the positions snapshot uses `previous_close` (not `prev_ltp`) as the close-price basis. Three test dimensions: (a) Source-scan `backend/api/routes/positions_helpers.py` — assert `actual_previous_close` variable exists and appears before `prev_ltp` in the `prev_close_val` computation; (b) Source-scan — assert `build_row_from_snapshot_raw` passes `previous_close=actual_previous_close` to `build_snapshot_position_row`; (c) API-level smoke — login as admin on `dev.ramboq.com`, fetch `/api/positions?fresh=1`, check that any rows returned have a non-null `day_change_val` when `previous_close` > 0. Pattern: follow `frontend/e2e/navstrip_pslot_closed_hours.spec.js` (source-scan + readFileSync approach for static assertions; Playwright page login for API smoke).
  2. `holdings_navstrip_ssot.spec.js` — Verifies that `pulseHoldingsStore` is included in `_tickBookPollers()` so NavStrip H:1 stays in sync with Pulse Holdings TOTAL. Three dimensions: (a) Source-scan `frontend/src/lib/data/marketDataStores.svelte.js` — assert `pulseHoldingsStore.load()` appears inside `_tickBookPollers`; (b) Source-scan `frontend/src/lib/PositionStrip.svelte` — assert `_liveHoldingsToday` reads from `pulseHoldingsStore.value`; (c) Source-scan — assert PositionStrip renders `dispHoldingsToday` in the H:1 slot.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
fix(ssot): positions snapshot uses previous_close for day P&L; pulseHoldingsStore in book poller

## Done when
- After MCX close, Pulse page Positions TOTAL day P&L shows non-zero (uses official previous_close)
- NavStrip P:1 slot matches Pulse positions TOTAL (both from positionsStore, book-poller loaded)
- NavStrip H:1 slot matches Pulse Holdings TOTAL (pulseHoldingsStore now in book poller)
- pytest passes including new `build_row_from_snapshot_raw` priority test
- svelte-check 0 errors
- Both new Playwright specs pass
- DATA_LIFECYCLE.md §4.4 priority corrected; PULSE_SPEC.md + NAVSTRIP_SPEC.md updated
