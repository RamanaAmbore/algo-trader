# Plan: Fix payoff zero line / pick-legs / hang on symbol switch + market open

## Task
Four reactive-tracking gaps that produce "zero line / pick legs / hang" when switching
between underlyings during MCX market hours, and "garbled" on tab return.

---

### Bug 1 — loadStrategy fires before `legs` is updated after symbol switch

**Root cause**:
`candidatePositions` (derived) filters by `selectedUnderlying`. The `legs`-update `$effect`
(line ~2370) depends on `candidatePositions`. The `loadStrategy trigger` `$effect` (line 1695)
depends on `selectedUnderlying`. Svelte 5 effects run in **declaration order**: the trigger
fires first (line 1695) with **stale CRUDEOIL legs** → `legsKey === _stratLastKey` → returns
early without fetching. The `legs`-update effect runs second → writes GOLDM legs. GOLDM strategy
never fetches until the next 5 s `marketAwareInterval` tick.

User symptom: switching CRUDEOIL→GOLDM shows "pick legs" for up to 5 s; rapid toggling
accumulates the wait and looks like a "hang".

**Fix**: Add a new `$effect` AFTER the legs-update effect (after line ~2389) that fires
`loadStrategy()` when `legs` changes AND strategy is stale for `selectedUnderlying`:

```js
// After the legs-update $effect (~line 2389):
$effect(() => {
  void legs;
  const sel = selectedUnderlying;
  const stratUnd = String(strategy?.underlying || '').toUpperCase();
  if (!legs.length) return;
  if (stratUnd && stratUnd === sel.toUpperCase()) return; // strategy already matches
  untrack(() => { try { loadStrategy(); } catch (_) {} });
});
```

The `_stratLastKey` memo inside `loadStrategy` prevents redundant network calls on every
positions-poll legs-update (legs key unchanged → returns immediately). The fetch only fires
when the legs key actually changed (different underlying's legs).

---

### Bug 2 — `_clientPayoffStub` spot resolver doesn't track `selectedUnderlying`

**Root cause (Gap A)**:
In `_clientPayoffStub`'s IIFE spot resolver, `selectedUnderlying` is read only inside
`untrack(() => _underlyingQuotes[selectedUnderlying]?.ltp)` (line 2532). If the `bqLtp`
early-return path fires (CRUDEOIL has a cached price), `selectedUnderlying` is never read
outside `untrack()` → NOT a reactive dep. When user switches to GOLDM, `_clientPayoffStub`
doesn't re-derive → shows CRUDEOIL spot → wrong/zero payoff stub.

**Fix**: capture `selectedUnderlying` before the first `untrack()` call:

```js
// BEFORE (line 2528–2540):
const spot = (() => {
  if (!isMarketOpen()) void _quoteGeneration;
  const bqLtp = untrack(() => _underlyingQuotes[selectedUnderlying]?.ltp);
  if (bqLtp != null && Number.isFinite(bqLtp) && bqLtp > 0) return bqLtp;
  const und = String(selectedUnderlying || '').toUpperCase();
  if (und) {
    const v = untrack(() => Number(getSnapshot(und)?.ltp));
    if (Number.isFinite(v) && v > 0) return v;
  }
  return 0;
})();

// AFTER:
const spot = (() => {
  void _quoteGeneration;            // Gap B fix (see below): always track
  const _sel = selectedUnderlying;  // Gap A fix: track symbol change regardless of early-return path
  const bqLtp = untrack(() => _underlyingQuotes[_sel]?.ltp);
  if (bqLtp != null && Number.isFinite(bqLtp) && bqLtp > 0) return bqLtp;
  if (_sel) {
    const v = untrack(() => Number(getSnapshot(_sel)?.ltp));
    if (Number.isFinite(v) && v > 0) return v;
  }
  return 0;
})();
```

---

### Bug 3 — `_quoteGeneration` not tracked during market hours

**Root cause (Gap B)**:
`if (!isMarketOpen()) void _quoteGeneration` (line 2003 in `liveSpot`, line 2531 in stub)
drops `_quoteGeneration` from the dep set when market is open. `loadUnderlyingQuotes` fires
every 5 s and increments `_quoteGeneration`, but neither `liveSpot` nor the stub re-derives.
During MCX pre-open (17:00–17:30) with sparse SSE ticks, `_throttledTick` doesn't increment
→ chart stuck showing stale/zero spot.

**Fix**: Remove the conditional — always track:

In `liveSpot` (~line 2003):
```js
// BEFORE:
if (!isMarketOpen()) void _quoteGeneration;
const bqLtp = untrack(() => _underlyingQuotes[selectedUnderlying]?.ltp);

// AFTER:
void _quoteGeneration;  // always track — batchQuote updates must re-trigger regardless of market state
const bqLtp = untrack(() => _underlyingQuotes[selectedUnderlying]?.ltp);
```

Update surrounding comment block to remove "Off-market only" phrasing.
The stub fix above already handles Gap B for `_clientPayoffStub`.

---

### Bug 4 — garbled on tab change / long inactivity (strategy wipe race)

**Root cause**:
On hibernation exit, `marketAwareInterval` edge fires `loadStrategy()` before the book poller
has refreshed positions. Stale positions with `qty=0` legs trigger the wipe at line 3816
(`strategy = null`) → chart blanks.

**Fix**: add a `_positionsRefreshedAt` freshness stamp and guard the wipe:

1. **Add state** (near other `let ... = $state()` declarations, ~line 491):
```js
let _positionsRefreshedAt = 0;  // epoch ms; updated after each successful loadPositions
```

2. **In loadPositions completion** (after `_positionsLoaded = true`, ~line 3754):
```js
_positionsLoaded      = true;
_positionsRefreshedAt = Date.now();
if (!positionsStore.error) lastRefreshAt.set(Date.now());
```

3. **Guard strategy wipe** (line 3816):
```js
// BEFORE:
if (!_hasEnabledLegs && strategy !== null && _positionsLoaded && instrumentsReady) strategy = null;

// AFTER:
const _positionsFresh = _positionsRefreshedAt > 0 && (Date.now() - _positionsRefreshedAt < 30_000);
if (!_hasEnabledLegs && strategy !== null && _positionsLoaded && instrumentsReady && _positionsFresh) strategy = null;
```

---

## Agents
- frontend: Implement all 4 fixes in `+page.svelte` as described above.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add 3 specs to `frontend/e2e/derivatives_reactive_chain.spec.js`:
  (1) Symbol switch CRUDEOIL→GOLDM: confirm GOLDM payoff renders within one tick, not 5 s.
  (2) Rapid symbol toggle (5 toggles): confirm no hang; final selection shows correct payoff.
  (3) Simulated hibernation exit with stale positions (>30 s): confirm strategy NOT wiped.

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Files changed
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — 6 edits
- `frontend/e2e/derivatives_reactive_chain.spec.js` — 3 new specs

## Done when
- Switching CRUDEOIL→GOLDM shows GOLDM payoff within one `_throttledTick` (≤250 ms)
- No zero line at MCX open or during pre-open sparse-tick period
- Rapid symbol toggle does not hang; correct payoff shown after settling on a symbol
- Strategy not wiped on hibernation exit when positions are stale (>30 s)
- `svelte-check 0 errors`, Playwright specs pass

## Commit message
fix(derivatives): fix loadStrategy ordering after symbol switch, track selectedUnderlying+_quoteGeneration in stub, guard strategy wipe
