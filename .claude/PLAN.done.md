# Plan: Fix underlyingSpotStore quote-wipe (cross-symbol staleness + tab-return blank)

## Context

Three bugs reported on the derivatives page, all caused by the same single line in `underlyingSpotStore.svelte.js`:

**Root cause — line 86**: `_quotes = next;`  
`loadUnderlyingSpots()` is called by two independent callers with different symbol sets:
- `PositionStrip._loadUnderlyingSpots()` — only fetches roots for F&O *option* positions (NSE/MCX CE/PE only, no futures)
- `derivatives.loadUnderlyingQuotes()` — fetches all underlyings in `_byUnderlyingTotals`

Every call does a **full replacement** of the shared `_quotes` object. When PositionStrip's fetch completes after the derivatives page's fetch (or vice versa), it wipes symbols the other caller populated.

**Bug 1 (cross-symbol staleness)**: GOLDM selected in payoff → PositionStrip only has CRUDEOIL options → its fetch completes and wipes GOLDM from `_quotes` → `_underlyingQuotes["GOLDM"]` is undefined → GOLDM snapshot row shows stale "—" while CRUDEOIL refreshes.

**Bug 2 (no pulse flash)**: When GOLDM is stale (Bug 1), `spot` on the payoff never changes → `_spotFlash.update('spot', spot)` never fires a direction change → no visible pulse. Expected to resolve as a symptom of Bug 1.

**Bug 3 (tab-return blank)**: On tab return, `visibleInterval` fires `loadUnderlyingQuotes` immediately → Bug 1 quote wipe → `liveSpot` resolves to 0 → `loadStrategy` fires with `spot=0` → strategy response may be degenerate → chart/legs/snapshot briefly empty until next correct quote cycle (~5s). Expected to resolve as a symptom of Bug 1.

## Task

Single-line fix in `underlyingSpotStore.svelte.js`: change full replacement to a merge so both callers' symbols coexist.

## Agents
- backend: skip
- frontend: In `frontend/src/lib/data/underlyingSpotStore.svelte.js` line 86, change `_quotes = next;` to `_quotes = { ..._quotes, ...next };`. This merges newly-fetched symbols into the existing map rather than replacing it, so PositionStrip's CRUDEOIL fetch no longer wipes GOLDM that derivatives page loaded (and vice versa). No other changes needed — the flash $effect at derivatives page line 922-929 iterates all entries in `_underlyingQuotes` via `Object.entries(quotes)` which is safe with extra keys; the tickBus `root in _underlyingQuotes` check at line 1655 only improves with more keys; `patchUnderlyingSpot` is a no-op for roots not in `_quotes` so extra keys are benign.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip
- frontend-test: In `frontend/src/lib/__tests__/data/underlyingSpotStore.test.js`, add one regression test: call `loadUnderlyingSpots` for `{root:"GOLDM", quoteKey:"MCX:GOLDM24OCTFUT"}`, then call it again for `{root:"CRUDEOIL", quoteKey:"MCX:CRUDEOIL24OCTFUT"}`, assert that both `GOLDM` and `CRUDEOIL` are present in `underlyingSpotStore.value` after the second call (the first call's entry must not be wiped). Mock `batchQuote` to return a minimal ltp response for each call.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no
- vitest: yes

## Commit message
fix(underlyingSpotStore): merge quotes instead of replacing — prevent cross-caller symbol wipe

## Done when
- `_quotes = next` on line 86 of underlyingSpotStore.svelte.js is replaced with `_quotes = { ..._quotes, ...next }`
- Regression test passes: two sequential `loadUnderlyingSpots` calls for different roots both survive in the store after the second call
- svelte-check 0 errors
- Vitest passes
- GOLDM and CRUDEOIL snapshot rows both refresh when GOLDM is active in payoff

## Critical files
- `frontend/src/lib/data/underlyingSpotStore.svelte.js` — line 86 (the fix)
- `frontend/src/lib/__tests__/data/underlyingSpotStore.test.js` — regression test
