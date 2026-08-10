# Plan: fix(navstrip): make _liveHoldingsTotal fully reactive to symbolStore LTP

## Context
NavStrip H slot 3rd value (`_liveHoldingsTotal`) shows 1.55c while MarketPulse holdings total shows 1.72c. Root cause confirmed: `_liveHoldingsTotal` wraps `getSnapshot(sym)?.ltp` in `untrack()`, preventing Svelte 5 from tracking `symbolStore` (a `SvelteMap`) as a reactive dependency. The `void _throttledTick` gate that was supposed to provide fallback reactivity only fires when `isMarketOpen()` is true, so outside market hours the value never updates. Independent TTL timers on `holdingsStore` vs `pulseHoldingsStore` cause the two computations to snapshot symbolStore at different times → divergent totals.

## Task
Remove `void _throttledTick` and `untrack()` from `_liveHoldingsTotal` in `PositionStrip.svelte` so the derived reads `symbolStore` directly and is fully reactive to every LTP update — matching how MarketPulse's holding rows recompute on tick changes.

## Agents
- frontend: In `frontend/src/lib/PositionStrip.svelte`, find `_liveHoldingsTotal` (around line 454). Change `const liveHold = untrack(() => getSnapshot(sym)?.ltp)` to `const liveHold = getSnapshot(sym)?.ltp` and remove the `void _throttledTick;` line at the top of that `$derived.by` block. No other changes — logic, fallback to `h?.pnl`, and qty formula stay identical. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional. `frontend/src/lib/*.svelte` change → add/update a Playwright spec in `frontend/tests/` covering the changed flow. The test should verify that the NavStrip H slot value matches the holdings pnl total rendered in MarketPulse (or at least that `_liveHoldingsTotal` reacts to a symbolStore LTP change without needing `_throttledTick` to fire).
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(navstrip): remove untrack() gate from _liveHoldingsTotal — fully reactive to symbolStore LTP

## Done when
`_liveHoldingsTotal` in PositionStrip.svelte reads `getSnapshot(sym)?.ltp` without `untrack()` and without `void _throttledTick`, svelte-check passes 0 errors, and the NavStrip H slot tracks MarketPulse holdings total in real time.
