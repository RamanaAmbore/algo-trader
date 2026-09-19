# Plan: SSOT fixes — spot refresh + chg% store

## Principle
The store computes the value. Every surface just reads it. No surface re-computes
what the store already provides.

## Two problems

### Problem 1 — Stale spot in layover and snapshot

`liveSpot` Tier 5 and the snapshot grid fall back to `_underlyingQuotes[sym]?.ltp`
(a secondary `$state` cache). The fix: read from `symbolStore` directly via
`getSnapshot(root)?.ltp` — symbolStore is already updated by both SSE ticks AND
`publishPulseQuotes` (which runs after every batchQuote).

### Problem 2 — chg% computed on every surface independently

Add `chg_pct` to `positionsDerivedStore.byKey[sym]` once. All consumers just read it —
no formula on any surface. `dayChangePct` helper lives only in `nav.js` and is used:
(a) inside `_computeDerived` to keep the formula in one place, and
(b) for aggregate TOTAL rows in PerformancePage + MarketPulse where you can't read
    per-symbol from the store.

---

## Changes

### 1. `frontend/src/lib/data/nav.js`
Add pure helper — formula lives here and nowhere else:
```js
export function dayChangePct(dayPnl, prevMv) {
  const dpnl = Number(dayPnl), mv = Number(prevMv);
  if (!Number.isFinite(dpnl) || mv <= 0) return null;
  return (dpnl / mv) * 100;
}
```

### 2. `frontend/src/lib/data/positionsDerivedStore.svelte.js`
Import `dayChangePct`. In the positions loop accumulate `prev_mv`; finalize `chg_pct`
after the loop using the helper:

```js
// In positions loop (after existing day_pnl accumulation):
const prev_close = Number(p?.previous_close) || Number(p?.close_price) || 0;
const avg        = Number(p?.average_price) || 0;
const refPx      = prev_close > 0 ? prev_close : avg;
const qty        = Number(p?.quantity) || 0;
if (!byKey[sym]) byKey[sym] = { day_pnl: 0, exp_pnl: null, extrinsic: null, pnl: 0, prev_mv: 0 };
byKey[sym].prev_mv += refPx * Math.abs(qty);

// After both loops, finalize:
for (const bk of Object.values(byKey)) {
  bk.chg_pct = dayChangePct(bk.day_pnl, bk.prev_mv);
}
```

`byKey[sym]` shape: `{ day_pnl, exp_pnl, extrinsic, pnl, prev_mv, chg_pct }`.
Existing `.day_pnl` consumers unaffected.

### 3. `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`
Read `chg_pct` from store. No formula on the surface:
```js
const _chgPct = $derived.by(() => {
  const stored = positionsDerivedStore.byKey[c.symbol]?.chg_pct;
  if (stored != null) return stored;
  if (c.chg_pct != null) return c.chg_pct;          // snapshot fallback
  const pc = c.prev_close ?? 0;
  if (typeof ltp === 'number' && pc > 0) return (ltp - pc) / pc * 100;  // ltp fallback
  return null;
});
```

### 4. `frontend/src/lib/data/pulseColumns.js`
Read `chg_pct` from store. Remove inline formula:
```js
function _dayPnlPctValueGetter(p) {
  const sym = String(p.data?.tradingsymbol || p.data?.symbol || '').toUpperCase();
  const stored = positionsDerivedStore.byKey[sym]?.chg_pct;
  if (stored != null) return stored;
  // fallback for holdings rows not covered by positionsDerivedStore
  const cp = Number(p.data?.change_pct);
  return Number.isFinite(cp) ? cp : null;
}
```
Import `positionsDerivedStore`.

### 5. `frontend/src/lib/PerformancePage.svelte`
Import `dayChangePct`. Replace two inline TOTAL row computations (only place the
helper is needed — aggregate totals can't read per-symbol from the store):
```js
day_change_percentage: dayChangePct(total_day_change, total_prev_val) ?? 0
```

### 6. `frontend/src/lib/MarketPulse.svelte`
Import `dayChangePct`. Update `_synthesiseTotalRow` positions path:
```js
t.day_change_percentage = dayChangePct(t.day_pnl, t.day_prev_val) ?? 0;
```

### 7. `frontend/src/lib/data/underlyingSpotStore.svelte.js`
Add import of `getSnapshot`. Fix `getUnderlyingSpot` to read symbolStore first:
```js
import { getSnapshot } from '$lib/data/symbolStore.svelte.js';
export function getUnderlyingSpot(root) {
  return getSnapshot(root)?.ltp || _quotes[root]?.ltp || 0;
}
```

### 8. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
**Tier 5 in `liveSpot`** — replace `_underlyingQuotes` read with symbolStore:
```js
const snapLtp = getSnapshot(selectedUnderlying)?.ltp;
if (snapLtp > 0) return snapLtp;
```

**Snapshot grid** — replace `_q.ltp` fallback with symbolStore:
```svelte
{@const _snapLtp = getSnapshot(g.underlying)?.ltp}
{@const _ltp = _useAnchor ? liveSpot : (_undLiveLtp[g.underlying] ?? (_snapLtp ?? null))}
```
Keep `_q` for `_close` and `_pct` (prev_close, day_pct not in symbolStore).

---

## Files to change
1. `frontend/src/lib/data/nav.js`
2. `frontend/src/lib/data/positionsDerivedStore.svelte.js`
3. `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`
4. `frontend/src/lib/data/pulseColumns.js`
5. `frontend/src/lib/PerformancePage.svelte`
6. `frontend/src/lib/MarketPulse.svelte`
7. `frontend/src/lib/data/underlyingSpotStore.svelte.js`
8. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

## Agents
- frontend: all 8 files; read MarketPulse lines 2460–2490 before editing;
  verify `getSnapshot` import already present in +page.svelte before adding

## Tests
- pytest: no
- svelte-check: yes
- vitest: yes

## Commit message
fix(derivatives): spot via symbolStore SSOT + chg% computed once in positionsDerivedStore

## Done when
- `positionsDerivedStore.byKey[sym].chg_pct` is the only place chg% is computed for positions
- Legs and Pulse read `chg_pct` from the store — no inline formula
- `dayChangePct` used only in `_computeDerived` and aggregate TOTAL rows
- `getUnderlyingSpot` and `liveSpot` Tier 5 read symbolStore — layover/snapshot spot refreshes on SSE tick
- svelte-check 0 errors, vitest green
