# Plan: Fix CRUDEOIL spot SSOT gap — derivatives Snapshot + payoff not updating per-tick

## Context
CRUDEOIL spot price updates live in MarketPulse positions grid but is frozen in the derivatives
Snapshot card (spot column) and payoff overlay. Both use `liveSpot` ($derived.by in +page.svelte).

Root cause: `patchUnderlyingSpot("CRUDEOIL", ltp)` IS called from the tick bus anchor path (Path 2)
whenever the anchor contract ticks, updating `underlyingSpotStore._quotes["CRUDEOIL"].ltp` and
therefore `_underlyingQuotes["CRUDEOIL"].ltp`. But `liveSpot` reads `_underlyingQuotes[selectedUnderlying]?.ltp`
inside `untrack()` and is only re-triggered by `_quoteGeneration` (incremented after each 30s batchQuote).
So tick-level patches to `_underlyingQuotes` are invisible to `liveSpot` until the next 30s poll.

Second gap: when `strategy.spot_anchor_contract` is null (sim mode, fallback), Path 2 never fires at
all for CRUDEOIL ticks — the underlying's nearest-future tradingsymbol (e.g., "CRUDEOILSEP26FUT")
has no handler in the tick bus.

## Task
Three targeted changes to `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

1. Add a reactive derived `_activeQuoteLtp` that tracks the selected underlying's LTP from
   `_underlyingQuotes`. This is reactive (not `untrack`-ed), so Svelte will re-derive it whenever
   `patchUnderlyingSpot` patches `_quotes`.

2. Add `void _activeQuoteLtp` in `liveSpot` so it re-derives whenever the patched LTP changes —
   closing the 30s-only update gap.

3. Add Path 3 in the tick bus handler: when neither Path 1 (direct root match) nor Path 2 (anchor)
   matched the ticked symbol, scan `_underlyingQuoteKeys` for a `quoteKey` whose tradingsymbol part
   equals `root`. If found, call `patchUnderlyingSpot(und, ltp)` + `flash.update`. Handles the case
   where `strategy.spot_anchor_contract` is null.

## Agents
- backend: skip
- frontend: Implement the three changes below in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add a Playwright smoke test for the derivatives page confirming the spot cell for the
  selected underlying renders a non-zero numeric value (covers the liveSpot derivation path end-to-end).

## Exact changes

### Change 1 — after line 865 (`let _quoteGeneration = $state(0);`)

Insert:
```javascript
  /** Reactive LTP of the currently selected underlying from underlyingSpotStore.
   *  Updates when patchUnderlyingSpot patches _quotes on each anchor/Path-3 tick,
   *  so liveSpot re-derives per-tick instead of waiting for the 30s _quoteGeneration bump. */
  const _activeQuoteLtp = $derived(_underlyingQuotes[selectedUnderlying]?.ltp ?? 0);
```

### Change 2 — in `liveSpot` at line 1725, after `void _throttledTick;`

Insert:
```javascript
    void _activeQuoteLtp;  // re-derive when tick-patched underlying LTP changes
```

### Change 3 — after the Path 2 anchor block (after line 1676, before the candidateLegRow loop)

Insert:
```javascript
      // Path 3: resolved quoteKey tradingsymbol — covers MCX when spot_anchor_contract is null.
      // Fires only when Path 1 (direct root key) and Path 2 (anchor) both missed this sym.
      if (!(root in _underlyingQuotes) && root !== _anchor) {
        for (const { root: und, quoteKey } of _underlyingQuoteKeys) {
          const _ts = quoteKey.includes(':') ? quoteKey.split(':')[1].toUpperCase() : '';
          if (_ts === root && und in _underlyingQuotes) {
            const _ps = getSnapshot(root);
            if (_ps?.ltp != null) {
              flash.update(`${und}:ltp`, Number(_ps.ltp));
              patchUnderlyingSpot(und, _ps.ltp);
            }
            break;
          }
        }
      }
```

## Files
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (lines 865, 1725, 1676)

## Reused utilities
- `patchUnderlyingSpot` from `frontend/src/lib/data/underlyingSpotStore.svelte.js` (already imported)
- `_underlyingQuoteKeys` derived (line 871) — already computed; reused in Path 3
- `flash.update` / `getSnapshot` — already available in tick bus handler scope

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes (smoke test — derivatives page loads and spot cell shows a value)

## Commit message
fix(derivatives): react to per-tick underlying spot patches — CRUDEOIL liveSpot SSOT gap

## Done when
- `_activeQuoteLtp` declared and tracked in `liveSpot`
- Path 3 in tick bus handler matches CRUDEOILSEP26FUT → CRUDEOIL and calls patchUnderlyingSpot
- svelte-check 0 errors
- Playwright smoke passes
