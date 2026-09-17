# Plan: Fix four derivatives/NavStrip/Pulse UX bugs

## Task

Fix four related bugs across the derivatives page, NavStrip, and Pulse page:

1. **No pulse flash (derivatives + Pulse change%)** — `createTickFlash.update()` advances `prev[key]` BEFORE the hidden-tab guard fires. While the tab is hidden, every poll silently catches `prev` up to the latest value. On tab-return, `v === prev[key]` → no change detected → no flash. Affects: derivatives Snapshot/legs/PayoffGreeks cells (poll-diff flash via `_mpFlash`) AND Pulse page change% column (same `createTickFlash` instance). Pulse LTP directional flash uses `tickBus` (separate mechanism, not broken by this bug).

2. **Tab-return blank (payoff / snapshot / legs)** — `loadStrategy()` line 3696 sets `loading = true` unconditionally. On tab-return, `marketAwareInterval` fires `loadStrategy()` immediately. If any leg qty changed while hidden (book poller ran), `legsKey !== _stratLastKey` → legs-signature memo doesn't skip → `loading = true` for 2-5s → payoff overlay, legs list, and snapshot all blank. The old strategy is already in memory and displayable; the refresh should be silent when a strategy exists.

3. **Stale spot (CRUDEOIL/GOLDM shows 9808)** — selecting an MCX underlying briefly shows the correct spot (from `strategy.spot` Tier-5 fallback or `candidatePositions.underlying_ltp` Tier-3), then `loadUnderlyingQuotes()` fires within 5s and writes the batchQuote result (stale MCX OHLC close = 9808) into `_underlyingQuotes[root]`. `_quoteGeneration++` triggers liveSpot re-derive. Tiers 1-3 fail (no SSE tick, no positions for this underlying). Tier 4 (batchQuote) returns 9808. Tier 5 (strategy.spot = correct) never reached. Root fix: swap Tier 4 and Tier 5 — `strategy.spot` is resolved by the backend using live KiteTicker data and refreshed every 5s; batchQuote for MCX runs at most every 30s and may return stale OHLC.close from the REST API.

4. **NavStrip zeros on mount / tab-return** — `PositionStrip.svelte` line 471: `let _prevMktOpen = false`. A `$effect` that watches `_mktTick` (fires every 30s AND immediately on tab-return) checks `if (open && !_prevMktOpen) → dispPositionsToday = 0; dispHoldingsToday = 0`. Because `_prevMktOpen` always starts as `false`, the very first effect run with market open triggers this "closed→open session reset" branch, zeroing both day P&L displays. Intended to wipe stale prior-session P&L at market open; fires falsely on every mount/tab-return because the initial value is `false` regardless of actual market state.

## Agents

- backend: skip
- frontend: Fix all four bugs — details below
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip (add Vitest unit test for createTickFlash hidden-tab behaviour instead)

### Frontend agent task

**File 1: `frontend/src/lib/data/tickFlash.svelte.js`**

Move the hidden-tab guard to the TOP of `update()`, before any `prev[key]` read or write. Current code advances `prev[key]` even when the document is hidden (line 38 before line 53 guard). After the fix: when hidden, skip entirely — `prev[key]` retains "the last value the user actually saw". The first visible tick after tab-return compares against that old value, detects the change, and flashes correctly.

Change the function body so the guard (`if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;`) is the FIRST check after validating `value` is a finite number — before `const last = prev[key]` and before `prev[key] = v`.

Update the existing comment block near the guard to reflect the new semantics: "skip entirely while hidden so prev[key] reflects the last value the user saw, not the last polled value."

This fix covers both surfaces: derivatives page poll-diff flash AND Pulse page change% flash (both use `createTickFlash`).

---

**File 2: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` line 3696**

Change `loading = true;` to `if (!strategy) loading = true;`.

When a valid strategy is already rendered, `loadStrategy()` refreshes silently in the background — no need to blank the UI. `loading = true` should only fire when there is nothing to show yet (initial load). The `_refreshing` variable used by RefreshButton is separate (line 4102) and unaffected.

---

**File 3: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — `liveSpot` $derived (lines ~1797–1825)**

Swap the order of Tier 4 (batchQuote) and Tier 5 (strategy.spot). After the swap:

- **New Tier 4** (`stratMatchesSel && strategy?.spot`): Strategy is re-fetched every 5s from the backend using live KiteTicker data — at most 5s stale during market hours. Check this before batchQuote.
- **New Tier 5** (batchQuote, `_underlyingQuotes[selectedUnderlying]?.ltp`): Fallback for when strategy hasn't loaded yet (page first-open, pre-market, no legs). Still needed for MCX pre-open window.

Exact change: move the block at lines 1823–1825 (`if (stratMatchesSel && strategy?.spot != null) { ... return strategy?.spot; }`) to BEFORE the `void _quoteGeneration; const bqLtp = ...` block (currently lines 1816–1821). Keep `_quoteGeneration` as a reactive dep (still needed for MCX pre-open where strategy is null). Update the Tier-4/5 comments to explain the reordering rationale.

---

**File 4: `frontend/src/lib/PositionStrip.svelte` line 471**

Change:
```javascript
let _prevMktOpen = false;
```
to:
```javascript
let _prevMktOpen = isNseOpen() || isMcxOpen();
```

`isNseOpen()` and `isMcxOpen()` are regular (non-reactive) functions already imported and called within this component; calling them at initialization is safe. This seeds `_prevMktOpen` from the actual current market state, preventing the `$effect` from seeing a false "closed→open" transition on first run.

---

**Vitest test** (`frontend/src/lib/__tests__/data/tickFlash.test.js` — create if not exists):

Add test: "hidden-tab: prev[key] not advanced while hidden; first visible update flashes"
- Set `document.visibilityState = 'hidden'`
- Call `flash.update('x', 100)` (initial seed) then `flash.update('x', 200)` (while hidden — should be skipped)
- Set `document.visibilityState = 'visible'`
- Call `flash.update('x', 200)` — should emit 'up' class (prev was still 100, not 200)
- Call `flash.update('x', 200)` again — no class (no change)

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(derivatives): fix pulse flash, tab-return blank, stale MCX spot, NavStrip zero on mount

## Done when

1. `createTickFlash.update()` skips entirely (does not advance `prev`) when document is hidden — derivatives and Pulse change% cells flash correctly on tab-return
2. Tab-return to derivatives page keeps existing chart/legs/snapshot visible during background refresh
3. Selecting CRUDEOIL or GOLDM does not replace the correct spot with a stale batchQuote value
4. PositionStrip NavStrip day P&L does not zero out on page mount or tab-return when market is open
5. svelte-check 0 errors
6. Vitest passes (including new hidden-tab test)
