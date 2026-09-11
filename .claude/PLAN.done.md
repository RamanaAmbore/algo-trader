# Plan: Fix MCX payoff chart blank during live market hours

## Context

The payoff chart works at 3:30 AM (MCX closed) but is blank during MCX market hours. Two confirmed root causes:

**Root cause A** — `tickBus` anchor bridge (added in 8ba24394) calls `flash.update()` for the LTP cell when the anchor-contract tick arrives, but never calls `applyUnderlyingTickLtp`. Result: `_underlyingQuotes["CRUDEOIL"].ltp` only gets updated by `loadUnderlyingQuotes()` batchQuote (every 5–30s), never from live SSE ticks.

**Root cause B** — `_clientPayoffStub`'s second spot fallback uses `getSnapshot(String(_sel).toUpperCase())?.ltp` = `getSnapshot("CRUDEOIL")?.ltp` which always returns `null` for MCX because symbolStore is keyed by the tradingsymbol `"CRUDEOILSEP26FUT"` not the root `"CRUDEOIL"`. When `_underlyingQuotes["CRUDEOIL"]` is also empty (instruments cache cold or batchQuote not yet run), `_clientPayoffStub` returns `[]`, legCount=0, and OptionsPayoff shows "Pick legs to see payoff." — chart blank.

At 3:30 AM, `_throttledTick` is silent (market closed), `liveSpot` reads only `_quoteGeneration`, batchQuote runs every 5s and populates `_underlyingQuotes["CRUDEOIL"]` — so tier 4 works. During market hours, execution diverges into a path where both tier 1a (requires `_underlyingQuotes` freshly updated) and the stub's fallback are broken.

## Task

Fix both root causes in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

1. In the `tickBus` anchor bridge block: after `flash.update(...)`, also call `applyUnderlyingTickLtp` so `_underlyingQuotes["CRUDEOIL"].ltp` is updated from live SSE ticks (root cause A).

2. In `_clientPayoffStub` spot fallback: replace `getSnapshot(String(_sel).toUpperCase())?.ltp` with a lookup via `resolveUnderlying(_sel, findNearestFuture)?.tradingsymbol` to get the actual futures tradingsymbol (e.g. "CRUDEOILSEP26FUT") that IS in symbolStore (root cause B).

## Agents

- backend: skip
- frontend: Fix two spots in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

  **Fix 1 — tickBus anchor bridge** (search for the block containing `flash.update(\`${_stratUnd}:ltp\``)):
  ```js
  // BEFORE:
  if (_anchor && root === _anchor && _stratUnd && _stratUnd in _underlyingQuotes) {
    const _as = getSnapshot(root);
    if (_as?.ltp != null) flash.update(`${_stratUnd}:ltp`, Number(_as.ltp));
  }

  // AFTER:
  if (_anchor && root === _anchor && _stratUnd && _stratUnd in _underlyingQuotes) {
    const _as = getSnapshot(root);
    if (_as?.ltp != null) {
      flash.update(`${_stratUnd}:ltp`, Number(_as.ltp));
      const _next = applyUnderlyingTickLtp(_underlyingQuotes, _stratUnd, _as.ltp);
      if (_next !== _underlyingQuotes) _underlyingQuotes = _next;
    }
  }
  ```

  **Fix 2 — `_clientPayoffStub` spot fallback** (search for `getSnapshot(String(_sel).toUpperCase())?.ltp` inside `_clientPayoffStub`):
  ```js
  // BEFORE:
  if (_sel) {
    const v = untrack(() => Number(getSnapshot(String(_sel).toUpperCase())?.ltp));
    if (Number.isFinite(v) && v > 0) return v;
  }

  // AFTER:
  if (_sel) {
    const _resolvedSym = untrack(() => resolveUnderlying(String(_sel).toUpperCase(), findNearestFuture)?.tradingsymbol);
    const _lookupSym = _resolvedSym || String(_sel).toUpperCase();
    const v = untrack(() => Number(getSnapshot(_lookupSym)?.ltp));
    if (Number.isFinite(v) && v > 0) return v;
  }
  ```

  Write or update a Vitest test in `frontend/src/lib/__tests__/` or a Playwright spec in `frontend/tests/` covering the changed logic. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(derivatives): MCX payoff chart blank during market hours — anchor bridge + stub spot key

## Done when

- `svelte-check` passes with 0 errors
- During MCX market hours, the payoff chart renders (liveSpot resolves from SSE ticks via the fixed anchor bridge, and the stub correctly looks up the futures tradingsymbol)
- The fix doesn't regress the off-market path (batchQuote-driven `_underlyingQuotes` still works at 3:30 AM)
