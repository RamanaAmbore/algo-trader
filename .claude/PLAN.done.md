# Plan: Fix chain-tab hang + holdings day P&L + setFromPulse throttle

## Root cause A — Chain tab / SymbolPanel backend hang (primary page-freeze cause)

`OptionChainTab` is embedded inside `SymbolPanel.svelte` (the modal that opens when clicking
any symbol in Pulse). When the modal opens, `OptionChainTab` mounts and immediately fires a
5-second polling loop for chain quotes. Each poll calls `broker.quote(80+ symbols)` via
`asyncio.to_thread` — **no timeout**. Hung calls park thread pool slots permanently.
After 2-3 stuck polls the thread pool fills → ALL `asyncio.to_thread` calls queue and never
run → ALL server routes appear to hang → page becomes completely unresponsive.

After closing the modal: polling stops but the stuck threads remain → backend still frozen.

## Root cause B — Holdings day P&L wrong (H pill slot 1 == slot 3)

H pill shows three values:
- Slot 1: `dispHoldingsToday` = `holdingsDayPnlStore.total` = `(liveLtp − closePx) × qty`
- Slot 2: `_liveHoldingsValue` = current market value
- Slot 3: `_liveHoldingsTotal` = `(liveHold − avgCost) × qty` (lifetime P&L)

Slot 1 matches slot 3 ← this only happens when `closePx == avgCost` (previous close ==
average purchase price). Root: `holdingsDayPnlStore` reads
`Number(h?.previous_close) || Number(h?.close_price) || 0`. If `previous_close` is absent
and `close_price` returns average_price from the backend holdings row, the formulas produce
identical results. Need to verify the backend holdings field and align `holdingsDayPnlStore`
to use the correct close field.

## Root cause C — setFromPulse throttle (frequent re-renders)

The MarketPulse `$effect` that calls `positionsDayPnlStore.setFromPulse` reads `unifiedRows`
which recomputes on every SSE quote tick (not throttled). This fires `setFromPulse` on every
tick, creating a new `_pulseByKey` object each time. `$state` assignment of a new object
triggers Svelte reactivity even when values haven't changed, causing PositionStrip and
NavCard to re-render at full SSE rate (~10 Hz) instead of 4 Hz. Fix: only call
`setFromPulse` when total actually changed.

## Task

Three-layer fix:
1. **Backend (chain-quotes)**: Add `asyncio.wait_for(timeout=10.0)` on `broker.quote()` in
   `_chain_quotes_batch_quote`. Add off-market gate: if market closed AND no cached response →
   return empty rows immediately. Add `_task_chain_instruments` background task (NFO+MCX only,
   T+30s) → chain-quotes prefers `instruments_chain` cache. Frontend expiry-retry loop.
2. **Frontend (holdings P&L)**: Fix `holdingsDayPnlStore` to read the correct close field.
   Investigate whether `previous_close` is absent in pulseHoldingsStore rows and whether
   `close_price` maps to average_price for holdings. Align with `_liveHoldingsTotal`'s data
   source (`pulseHoldingsStore`) and verify the field names against actual holdings API response.
   Also fix PositionStrip `_liveHoldingsTotal` if it uses wrong formula for slot 3.
3. **Frontend (throttle)**: In the MarketPulse `$effect`, only call `setFromPulse` when
   `Math.round(pulseTotal * 100) !== Math.round((_lastPulseTotal ?? NaN) * 100)` (paisa-level
   change gate) using a module-level `_lastPulseTotal` variable. This keeps the update path
   correct while eliminating no-op state writes on every tick.

## Agents

- backend:
  1. `backend/api/routes/options.py` — `_chain_quotes_batch_quote` (line 2221): wrap
     `asyncio.to_thread(broker.quote, keys)` with `asyncio.wait_for(..., timeout=10.0)`.
     Catch `asyncio.TimeoutError` → log warning + return `{}`.
  2. `chain_quotes` route off-market gate (around line 2660): after the closed-cache miss,
     return `ChainQuotesResponse(underlying=und, expiry=exp, expiries=all_expiries, rows=[])`
     instead of falling through to `broker.quote()`.
  3. `chain_quotes` route: use `peek("instruments_chain") or peek("instruments")`.
  4. `backend/api/routes/instruments.py`: add `_fetch_chain_instruments()` using
     `_fetch_exchange_raw("NFO", kite_accts)` + `_fetch_exchange_raw("MCX", kite_accts)`.
  5. `backend/api/background.py`: add `_task_chain_instruments()` (T+30s, while True loop,
     daily 08:02 IST). Register as `bg-chain-instruments` in on_startup.

- frontend:
  1. `frontend/src/lib/order/OptionChainTab.svelte`:
     - Guard quote effect (line 454): `if (!chainExpiry) return;` at the top
     - Add expiry-fetch retry: poll every 5s up to 12 times (60s) when expiries empty
  2. `frontend/src/lib/data/holdingsDayPnlStore.svelte.js`:
     - Read the backend holdings API response (check pulseHoldingsStore field names).
     - The close field priority should be: `previous_close` → `close_price` (correct per CLAUDE.md).
     - If the API returns `ohlc.close` instead of `close_price` for holdings, update the
       field reference accordingly.
     - Ensure the formula `(liveLtp − closePx) × heldQty` uses the PREVIOUS SESSION close,
       not the average purchase price. If `closePx` is 0 or equals average_price, log a
       warning and fall back to `dcv` (don't compute from 0-close).
  3. `frontend/src/lib/MarketPulse.svelte`:
     - Add `let _lastPulseTotal = null;` (module-level or component-level state).
     - In the `$effect` that calls `setFromPulse`: wrap the call with
       `if (Math.round(pulseTotal * 100) !== Math.round((_lastPulseTotal ?? NaN) * 100)) {`
       `  _lastPulseTotal = pulseTotal; positionsDayPnlStore.setFromPulse(pulseByKey, pulseTotal); }`
       This prevents no-op state writes on every SSE tick.

- broker: skip
- doc: skip
- backend-test:
  1. Test `_chain_quotes_batch_quote` returns `{}` on `asyncio.TimeoutError`
  2. Test off-market gate returns empty rows on cache miss (no broker.quote call)
  3. Test `_fetch_chain_instruments` returns only NFO+MCX rows
  File: `backend/tests/test_chain_quotes.py`
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(chain+holdings): broker.quote timeout + off-market guard + holdings day P&L + setFromPulse throttle

## Done when
- Chain tab / SymbolPanel no longer hangs the backend — broker.quote times out in ≤10s
- Off-market: chain returns expiries + empty rows immediately (no broker call)
- H pill slot 1 shows correct day P&L (different from slot 3 lifetime P&L)
- PositionStrip no longer re-renders at SSE tick rate — setFromPulse only fires on value change
- Chain expiries available within 30s of restart (NFO+MCX cache at T+30s)
- pytest passes; svelte-check 0 errors

## OOM safety
- T+30s NFO+MCX: sparkline's NFO download coalesced via @ssot_fetch — safe, ONE download
- No timeout_seconds on instruments download — OOM invariant from 2026-07-25
- _task_chain_instruments MUST have while True loop — supervised tasks must park, not return
