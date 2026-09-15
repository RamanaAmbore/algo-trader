# Plan: Fix MCX virtual-root batchQuote subscription + Legs/Spot LTP text color

## Plan A — MCX subscription bug (ship first, separate commit)

### Context

**Root cause (frontend)**: `_underlyingQuoteKeys` ($derived.by at +page.svelte:870) is
computed at render time, before instruments load. `findNearestFuture("CRUDEOIL")` returns
null (instruments.js `_byUnderlyingType` is a plain `let`, not `$state` — reading it
inside a $derived gives no reactivity). `resolveUnderlying` falls back to
`quoteKey = "MCX:CRUDEOIL"` (synthetic virtual root). Since `_underlyingQuoteKeys`
doesn't read `instrumentsReady`, it **never re-derives** when instruments load.
Every subsequent 30 s batchQuote call keeps sending "MCX:CRUDEOIL".

Two things fail downstream from this wrong quoteKey:

1. **Backend subscription gap** — `batch_quote` builds `seen_pairs` from the original key:
   `("MCX", "CRUDEOIL")`. Token map has no entry for the bare root; only
   "CRUDEOIL26OCTFUT" has a token. `_subscribe_batch_universe_to_ticker` finds nothing
   → no KiteTicker subscription → CRUDEOIL26OCTFUT never receives SSE ticks.

2. **byKey mismatch in `loadUnderlyingSpots`** — batchQuote response items carry
   `tradingsymbol: "CRUDEOIL26OCTFUT"` (resolved by backend). `byKey` is indexed by
   `"MCX:CRUDEOIL26OCTFUT"`, but the lookup key is `quoteKey = "MCX:CRUDEOIL"` →
   no match → `_quotes["CRUDEOIL"]` never set → `_underlyingQuotes["CRUDEOIL"]`
   undefined → tier 4 of `liveSpot` (batchQuote fallback) also fails.

**Why "page refresh fixes it"**: `liveSpot` falls through all four tiers and returns
`strategy.spot` — the underlying spot fetched from the broker REST API at analytics
load time (every page-load calls `loadStrategy()`). This looks "current" immediately
after refresh but drifts as the session continues.

**Cascading to NavStrip**: During MCX-only hours (NSE closed), if no MCX symbol is
subscribed to KiteTicker, `symbolTickCount` never increments → all tick-gated stores
freeze: `holdingsDayPnlStore`, `positionsDayPnlStore`, and `liveSpot`'s
`_throttledTick`. Even with NSE open the issue persists because `liveSpot` tier 1a/2
fail (CRUDEOIL26OCTFUT not in symbolStore) and tier 4 also fails (byKey mismatch).

### Fix A1 — Frontend: `_underlyingQuoteKeys` instruments dependency
**File**: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` line ~870

Add `void instrumentsReady;` as the first line of the `_underlyingQuoteKeys`
`$derived.by` body. When instruments load, `instrumentsReady` becomes true →
the derived re-computes → `findNearestFuture("CRUDEOIL")` now returns
"CRUDEOIL26OCTFUT" (instruments are loaded) → `quoteKey = "MCX:CRUDEOIL26OCTFUT"`.

**Before:**
```javascript
const _underlyingQuoteKeys = $derived.by(() => {
  const out = [];
  for (const g of _byUnderlyingTotals) {
    const r = resolveUnderlying(g.underlying, findNearestFuture);
    if (r?.quoteKey) out.push({ root: g.underlying, quoteKey: r.quoteKey });
  }
  return out;
});
```

**After:**
```javascript
const _underlyingQuoteKeys = $derived.by(() => {
  void instrumentsReady; // re-derive after instruments load so findNearestFuture resolves MCX contracts
  const out = [];
  for (const g of _byUnderlyingTotals) {
    const r = resolveUnderlying(g.underlying, findNearestFuture);
    if (r?.quoteKey) out.push({ root: g.underlying, quoteKey: r.quoteKey });
  }
  return out;
});
```

Effect: the 30 s batchQuote poll (via `visibleInterval` at line 3951) uses the
re-derived quoteKey "MCX:CRUDEOIL26OCTFUT" → backend `seen_pairs` gets the real
contract ("MCX","CRUDEOIL26OCTFUT") → token found → KiteTicker subscribed → SSE
ticks flow → `getSnapshot("CRUDEOIL26OCTFUT")` returns live LTP → `liveSpot` tier 1a
resolves in real-time.

The seed call at line 3899 fires synchronously after `instrumentsReady = true` before
Svelte's microtask flushes the $derived — it still uses the old quoteKey. This is
acceptable: the first 30 s batchQuote call (the throttled interval) will use the
correct key. The seed call is best-effort only.

### Fix A2 — Backend resilience: `backend/api/routes/quote.py` — batch_quote handler

Defensive fix for cases where the frontend still sends virtual roots (seed call, other
callers). In the `for k in keys` loop (around line 783), move `broker_key` computation
before `seen_pairs.append` and use the resolved broker key:

**Before:**
```python
exch, sym = k.split(":", 1)
seen_pairs.append((exch.upper(), sym.upper()))

# Record LKG for closed-hours fallback.
broker_key = key_map.input_to_broker.get(k, k)
```

**After:**
```python
exch, sym = k.split(":", 1)
# Use resolved broker key for ticker subscription — virtual MCX/CDS roots
# (e.g. "MCX:CRUDEOIL") have no instrument token; only the actual front-month
# contract (e.g. "MCX:CRUDEOIL26OCTFUT") can be subscribed.
broker_key = key_map.input_to_broker.get(k, k)
bk_exch, bk_sym = broker_key.split(":", 1) if ":" in broker_key else (exch, sym)
seen_pairs.append((bk_exch.upper(), bk_sym.upper()))
```

Remove the now-duplicate `broker_key` assignment below. Keep `_record_live_batch_lkg`
unchanged.

### Tests
- **vitest**: add test for `_underlyingQuoteKeys` — when `instrumentsReady` is false,
  quoteKey should be virtual ("MCX:CRUDEOIL"); when true (instruments loaded with MCX
  futures), quoteKey should resolve to "MCX:CRUDEOIL26OCTFUT". This can be a unit test
  in `frontend/src/lib/__tests__/` targeting `resolveUnderlying` + `findNearestFuture`
  with a stub instruments map.
- **pytest**: add test asserting that batch_quote with "MCX:CRUDEOIL" (virtual root)
  causes `_subscribe_batch_universe_to_ticker` to receive `("MCX","CRUDEOIL26OCTFUT")`
  in `seen_pairs`, not `("MCX","CRUDEOIL")`. Mock `_subscribe_batch_universe_to_ticker`
  to capture the argument.

### Agents
- **frontend**: Apply Fix A1 (add `void instrumentsReady` to `_underlyingQuoteKeys`).
- **backend**: Apply Fix A2 (seen_pairs uses resolved broker key in quote.py).
- **backend-test**: Add pytest for Fix A2.
- All others: skip.

Note: Fix A1's unit test should be added by the frontend agent using an existing
instruments stub pattern (grep `__tests__` for any instruments mock to reuse).

### Commit message
fix(derivatives): resolve MCX futures contract for batchQuote subscription and spot-quote key

### Done when
1. After instruments load (within ~30 s of page open), CRUDEOIL spot in derivatives
   page Snapshot and payoff updates in real-time without page refresh
2. NavStrip values (holdings P&L, positions day P&L) update during MCX-only hours
3. pytest green, svelte-check 0 errors, vitest green

---

## Plan B — Legs/Spot LTP text color (separate commit after Plan A ships)

### Context

Currently, Legs LTP has a left vertical bar that is color-coded. User wants the **LTP
text itself** to be colored green/red based on `ltp > prev_close` (not the bar). Same
treatment for the Spot LTP in the Snapshot card of the derivatives page.

### Fixes

**File 1**: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`
- Find the LTP span in the template
- Compute `_ltpVsClose = (c.ltp != null && c.prev_close > 0) ? (c.ltp > c.prev_close ? 'cell-pos' : c.ltp < c.prev_close ? 'cell-neg' : 'cell-flat') : ''`
- Apply `class={_ltpVsClose}` to the LTP value span
- Remove/keep the vertical bar as appropriate (user said "instead", so remove the bar; add the color to the LTP text)

**File 2**: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- Find the Spot LTP display in the Snapshot card
- `spotPrevClose` comes from `_underlyingQuotes[selectedUnderlying]?.prev_close` or strategy `spot_prev_close`
- Compute `_spotDir = (liveSpot > 0 && spotPrevClose > 0) ? (liveSpot > spotPrevClose ? 'cell-pos' : liveSpot < spotPrevClose ? 'cell-neg' : 'cell-flat') : ''`
- Apply `class={_spotDir}` to the Spot LTP text span

### Agents
- **frontend**: Apply LTP text color changes in CandidateLegRow.svelte and +page.svelte.
- All others: skip.

### Tests
- svelte-check: yes
- pytest: no
- vitest: no

### Commit message
feat(derivatives): color-code LTP text by prev_close direction in Legs and Spot

### Done when
1. Legs LTP text is green when ltp > prev_close, red when ltp < prev_close
2. Spot LTP text is green/red based on same rule
3. svelte-check: 0 errors
