# Plan: Sync payoff overlay spot price with day P&L (reduce underlying quote stale)

## Context

The payoff chart overlay (top-left corner, `OptionsPayoff.svelte:717-805`) shows two values side-by-side:
- **SPOT** — reads `getSnapshot(anchor)?.ltp` in `liveSpot` (`+page.svelte:1874-1919`)
- **DAY P&L** — reads per-leg SSE deltas in `candidatesDayPnl` (`+page.svelte:1936-1955`)

These two values read from different data sources at different freshness levels:

| Value | Source | Cadence |
|---|---|---|
| Spot (`liveSpot`) | `getSnapshot(anchor)?.ltp` → seeded by `loadUnderlyingQuotes → publishPulseQuotes` | **30s poll** (when underlying not in SSE ticker) |
| Day P&L base (`_dayPnlForLeg`) | `getSnapshot(c.symbol)?.ltp` for each option leg | **250ms** (option legs subscribed in SSE as held positions) |
| Day P&L SSE delta | `(liveLtp - pollLtp) * qty` per leg from `getSnapshot(c.symbol)` | **250ms** |

Option legs (NIFTY24500CE etc.) are subscribed to the SSE ticker because they are held positions — their LTPs tick in real-time. The underlying index/anchor (NIFTY 50, BANKNIFTY) may NOT be subscribed to SSE. In that case, `getSnapshot(anchor)` only gets updated when `loadUnderlyingQuotes()` runs and calls `publishPulseQuotes`, which seeds `symbolStore` for the anchor with the batch-quote LTP.

**Result:** Day P&L updates at 250ms (SSE-driven), spot updates at 30s (batch-quote-driven). Operator sees day P&L jumping while spot lags behind.

**Fix:** Decouple `loadUnderlyingQuotes` from `loadSimStatus` and reduce its poll interval from 30s → 5s. This makes `getSnapshot(anchor)?.ltp` → `liveSpot` fresh within 5s, reducing the visible desync from "30s vs 250ms" to "5s vs 250ms" (far less jarring).

---

## Task

Change `+page.svelte:3947-3950` to split `loadUnderlyingQuotes` and `loadSimStatus` back into separate timers:
- `loadUnderlyingQuotes`: every 5s (same cadence as `loadStrategy` at line 3942), 10s when tab is hidden
- `loadSimStatus`: keep at 30s

Critical constraint: `marketAwareInterval` is the correct wrapper (self-pauses during market-closed hours). Do NOT use plain `setInterval`.

Also: in the `tickBus.subscribe` handler (`+page.svelte:1835-1852`), when a tick arrives for `root in _underlyingQuotes` AND `getSnapshot(root)?.ltp` is valid, also write `_underlyingQuotes[root] = { ..._underlyingQuotes[root], ltp: Number(snap.ltp) }`. This makes the Snapshot grid Spot cells update immediately on any SSE tick for the underlying (including infrequent index ticks), not just on 5s polls.

---

## Agents

- frontend: In `/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:
  
  **Change 1 — Split the merged timer (lines ~3946-3950):**
  
  Current:
  ```javascript
  // Fix 8: loadUnderlyingQuotes + loadSimStatus merged into one 30s timer.
  quotesTeardown = marketAwareInterval(
    () => Promise.allSettled([loadUnderlyingQuotes(), loadSimStatus()]),
    30000, 30_000,
  );
  ```
  
  Replace with two separate timers:
  ```javascript
  // Underlying quotes at 5s — same cadence as loadStrategy — so liveSpot
  // in the payoff overlay stays within 5s of market price even when the
  // underlying (e.g. NIFTY 50 index) is not in the SSE ticker subscription.
  quotesTeardown = marketAwareInterval(loadUnderlyingQuotes, 5000, 10_000);
  // Sim status is cheap but doesn't need sub-minute freshness.
  simTeardown = marketAwareInterval(loadSimStatus, 30000, 30_000);
  ```
  
  Also: declare `let simTeardown;` near where `quotesTeardown` is declared. Add `simTeardown?.()` to the teardown cleanup wherever `quotesTeardown?.()` is called.
  
  **Change 2 — Live-update `_underlyingQuotes` from `tickBus` (lines ~1839-1842):**
  
  Current:
  ```javascript
  if (root in _underlyingQuotes) {
    const snap = getSnapshot(root);
    if (snap?.ltp != null) flash.update(`${root}:ltp`, Number(snap.ltp));
  }
  ```
  
  Extend:
  ```javascript
  if (root in _underlyingQuotes) {
    const snap = getSnapshot(root);
    if (snap?.ltp != null) {
      flash.update(`${root}:ltp`, Number(snap.ltp));
      // Keep _underlyingQuotes current on any SSE tick for the underlying
      // so the Snapshot grid Spot column reflects real-time moves without
      // waiting for the 5s loadUnderlyingQuotes poll.
      const prev = _underlyingQuotes[root];
      if (prev) _underlyingQuotes[root] = { ...prev, ltp: Number(snap.ltp) };
    }
  }
  ```

  **Write test:** Create or update `frontend/src/lib/__tests__/data/pairModal.test.js` OR create a new test file `frontend/src/lib/__tests__/data/underlyingQuotes.test.js`. Extract the `_underlyingQuotes` live-update logic into a pure helper function `applyUnderlyingTickLtp(quotes, root, ltp)` that returns a new quotes object with the updated LTP, and test it with Vitest:
  - `applyUnderlyingTickLtp({'NIFTY 50': {ltp:100, day_pct:0, prev_close:99}}, 'NIFTY 50', 101)` → `{ltp:101, day_pct:0, prev_close:99}`
  - returns unchanged object when root not in quotes
  - returns unchanged object when ltp is null/NaN
  
  Since `applyUnderlyingTickLtp` is a pure helper, extract it into `frontend/src/lib/data/underlyingQuoteUtils.js` (new file) and import it in `+page.svelte`. Test the exported function in `underlyingQuotes.test.js`.

  Run `cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1` at the end to confirm 0 errors.

- backend: skip
- backend-test: skip
- doc: skip (minor tuning, no spec change)
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): sync payoff overlay spot + day P&L — underlying quotes poll 30s→5s + tick live-update

## Done when
- `loadUnderlyingQuotes` runs every 5s (separate timer from `loadSimStatus`)
- `_underlyingQuotes` updates on any SSE tick for the underlying, not just on polls
- Spot price in payoff overlay visibly refreshes within 5s of underlying price move
- svelte-check 0 errors
- Vitest tests for `applyUnderlyingTickLtp` pass
