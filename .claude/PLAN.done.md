# Plan: Fix Derivatives Snapshot SSOT — Day P&L and Exp P&L (Strict)

## Context

After the `positionsDerivedStore` refactor (commit `ce2e568c`) unified P&L signals into a global store, the Derivatives page Snapshot panel broke in three ways:

1. **Day P&L = 0** — `_fnoDayPnlByRoot` reads `positionsDerivedStore.byKey` (last-write-wins per symbol); multi-account positions overwrite each other, losing rows whose last entry had `dcv=0`.
2. **Exp P&L mismatch (-4.64L vs -2.84L)** — `positionsDerivedStore.byRootPositions[root].exp_pnl` uses only 2 spot fallbacks (`underlying_ltp → getUnderlyingSpot`), no SSE tick chain, and no `legAnalyticsBySymbol`. Legs uses `_legsExpPnlTotal` with `liveSpot` (4-tier SSE) + `legAnalyticsBySymbol`.
3. **Snapshot TOTAL Exp** reads `positionsDerivedStore.total.exp_pnl` — global, unfiltered, different from the per-row sum visible above it.

**Operator requirement: strict SSOT** — the Snapshot row for the *selected* underlying must show the **exact same number** as the Legs TOTAL for both Day P&L and Exp P&L. For other roots, use filter-aware `_perRootReduce`.

## Task

In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

1. **Hoist `_legsDayPnlTotal`** to script level (same formula as template `_totalDcv`). Replace the template's inline `_totalDcv` with it.
2. **Replace `_fnoDayPnlByRoot`** with `_dayPnlByRootMap` via `_perRootReduce` (filter-aware, multi-account correct).
3. **Restore `_expPnlByRootMap`** via `_perRootReduce` with a `_rawPosExpPnl` helper (uses `_rootSpot` 4-tier, `legAnalyticsBySymbol`).
4. **Snapshot per-row**: for the selected underlying, read `_legsDayPnlTotal` / `_legsExpPnlTotal` directly; for all other roots, read from the `_perRootReduce` maps.
5. **Snapshot TOTAL**: `_legsDayPnlTotal + Σ_dayPnlByRootMap[root≠selected]` and `_legsExpPnlTotal + Σ_expPnlByRootMap[root≠selected]`.
6. **Write Vitest tests** for `_rawPosExpPnl` — extract to `derivativesMath.js` and export.

## Agents

- frontend: Implement all changes to `+page.svelte` + extract `_rawPosExpPnl` to `derivativesMath.js`
- backend-test: Add Vitest tests for `_rawPosExpPnl` in `frontend/src/lib/__tests__/data/derivativesMath.test.js` (or existing file)
- backend: skip
- broker: skip
- doc: skip
- playwright: skip

## Spec

### 1. Hoist `_legsDayPnlTotal` to script level

Add near `_legsExpPnlTotal` (line ~1975):

```javascript
/** Day P&L TOTAL for the currently selected underlying across all enabled
 *  F&O legs — script-level SSOT shared by the Legs TOTAL row AND the
 *  Snapshot row for the selected underlying so both surfaces always show
 *  an identical number. Excludes equity legs (kind === 'eq'). */
const _legsDayPnlTotal = $derived.by(() =>
  displayedCandidates
    .filter(c => _isLegEnabled(c) && c.kind !== 'eq')
    .reduce((s, c) => s + _candDayPnl(c), 0)
);
```

In the template TOTAL row (line ~4609), replace the inline `_totalDcv` definition:

```svelte
// Remove: {@const _totalDcv = _selectedCands.filter(...).reduce(...)}
// Change all _totalDcv references to _legsDayPnlTotal
```

---

### 2. `_rawPosExpPnl` helper → extract to `derivativesMath.js`

Add and export from `frontend/src/lib/data/derivativesMath.js`:

```javascript
/**
 * Exp P&L for a raw position row (fields: .quantity / .average_price / .tradingsymbol).
 * Used by _expPnlByRootMap via _perRootReduce.
 * @param {any} c - raw position row with kind added by perRootReduce
 * @param {number|null} spot
 * @param {Record<string,{strike?:number,opt_type?:string}>} [legAnalytics]
 * @returns {number|null}
 */
export function rawPosExpPnl(c, spot, legAnalytics = {}) {
  const sym      = String(c.tradingsymbol || c.symbol || '').toUpperCase();
  const qty      = Number(c.quantity      ?? 0);
  const avg      = Number(c.average_price ?? 0);
  const realised = Number(c.realised      ?? 0);
  const pnl      = Number(c.pnl          ?? 0);
  if (qty === 0) return realised || pnl;
  if (c.kind === 'fut') {
    const live = Number(c.last_price ?? 0);
    return live > 0 ? (live - avg) * qty + realised : null;
  }
  if (spot == null || spot <= 0) return null;
  const ev = expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: c.kind }, spot, legAnalytics);
  return ev != null ? ev + realised : null;
}
```

Import `rawPosExpPnl` in `+page.svelte` and use it:

```javascript
// In +page.svelte, near _pnlByRootMap:
const _expPnlByRootMap = $derived.by(() => {
  const ms = _makeStrategyMatcher();
  return _perRootReduce((c, spot) => rawPosExpPnl(c, spot, legAnalyticsBySymbol), ms);
});
```

---

### 3. Replace `_fnoDayPnlByRoot` with `_dayPnlByRootMap`

Add near `_pnlByRootMap` (line ~849), **remove** the `_fnoDayPnlByRoot` block (lines 1055–1067) entirely:

```javascript
const _dayPnlByRootMap = $derived.by(() => {
  const ms   = _makeStrategyMatcher();
  const open = isMarketOpen();
  return _perRootReduce((c, _spot) => {
    const sym  = String(c.tradingsymbol || c.symbol || '').toUpperCase();
    const snap = untrack(() => getSnapshot(sym));
    return livePositionDayPnl(
      {
        closePx: Number(c.previous_close) || Number(c.close_price  ?? 0),
        pollLtp: Number(c.last_price      ?? 0),
        qty:     Number(c.quantity        ?? 0),
        avg:     Number(c.average_price   ?? 0),
        dcvRow:  c,
      },
      snap?.ltp ?? null,
      { marketOpen: open },
    );
  }, ms);
});
```

---

### 4. Remove stale comments + update `_snapshotTotalExp`

Remove comment block (lines ~850–858):
```
// _expPnlByRootMap removed — ...
// Snapshot TOTAL sums — P&L uses _pnlByRootMap ...; Exp uses positionsDerivedStore.total.exp_pnl (global, unfiltered).
const _snapshotTotalExp = $derived(positionsDerivedStore.total.exp_pnl);
```

Replace with:

```javascript
// Snapshot TOTAL sums. For the selected underlying, uses the Legs-TOTAL
// script-level deriveds (_legsDayPnlTotal / _legsExpPnlTotal) so the
// highlighted Snapshot row and the Legs TOTAL row are always identical.
// For all other roots, uses the _perRootReduce maps (filter-aware, 4-tier spot).
const _snapshotTotalDay = $derived.by(() => {
  let sum = _legsDayPnlTotal;
  for (const [root, v] of Object.entries(_dayPnlByRootMap)) {
    if (root !== selectedUnderlying) sum += Number(v || 0);
  }
  return sum;
});
const _snapshotTotalExp = $derived.by(() => {
  let sum = _legsExpPnlTotal;
  for (const [root, v] of Object.entries(_expPnlByRootMap)) {
    if (root !== selectedUnderlying) sum += Number(v || 0);
  }
  return sum;
});
```

---

### 5. Template changes — Snapshot per-row

Replace (lines ~4777–4779):

```svelte
{@const _dayVal = g.underlying === selectedUnderlying
  ? _legsDayPnlTotal
  : (_dayPnlByRootMap[g.underlying] ?? 0)}
{@const _pnlVal = _pnlByRootMap[g.underlying] ?? 0}
{@const _expVal = g.underlying === selectedUnderlying
  ? _legsExpPnlTotal
  : (_expPnlByRootMap[g.underlying] ?? 0)}
{@const _extVal = positionsDerivedStore.byRootPositions[g.underlying]?.extrinsic ?? 0}
```

Replace TOTAL row (lines ~4807–4809):

```svelte
<span ...>{aggCompact(_snapshotTotalDay)}</span>
<span ...>{aggCompact(_snapshotTotalPnl)}</span>
<span ...>{aggCompact(_snapshotTotalExp)}</span>
```

Remove stale comment at lines ~4629–4631 ("_legsExpPnlTotal is the script-level SSOT shared with the snapshot row...") — replace with accurate comment:

```
<!-- Strict SSOT: Snapshot row for selectedUnderlying reads _legsDayPnlTotal /
     _legsExpPnlTotal directly — identical to the Legs TOTAL row above.
     Other roots read from _dayPnlByRootMap / _expPnlByRootMap (_perRootReduce). -->
```

---

### 6. Tests — `rawPosExpPnl` in derivativesMath.test.js

Add to `frontend/src/lib/__tests__/data/derivativesMath.test.js` (create if needed):

1. Option with valid spot → `(intrinsic - avg) * qty + realised`
2. Short option (negative qty) → correct sign
3. Future with `last_price` → `(ltp - avg) * qty + realised`
4. Closed leg (`qty=0`) → `realised || pnl`
5. Option with `spot=0` → null
6. Option with `spot=null` → null
7. Uses `legAnalytics` strike when provided (skips regex parse)

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(derivatives): strict SSOT — Snapshot selected-root row reads _legsDayPnlTotal/_legsExpPnlTotal directly; other roots via _perRootReduce with 4-tier spot + account filter

## Done when

- Snapshot Day P&L and Exp P&L for the selected underlying **exactly match** Legs TOTAL row
- Snapshot Day P&L and Exp P&L for other roots use filter-aware `_perRootReduce` with `_rootSpot` 4-tier spot
- Snapshot TOTAL = `_legsDayPnlTotal + Σ other roots` (consistent with per-row values)
- `svelte-check` 0 errors
- `npx vitest run` passes (1031+ tests, 7 new `rawPosExpPnl` cases)
