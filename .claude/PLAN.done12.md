# Plan: Add fallback param to store .get() methods; eliminate ?? 0 at call sites

## Context

`holdingsDayPnlStore.get(sym)` and `positionsDerivedStore.get(sym)` return objects
with nullable fields. Every arithmetic call site appends `?? 0`, pushing defensive
defaults to consumers. Adding `fallback = null` as a second param lets callers declare
intent (`get(sym, 0)`) and eliminates scattered `?? 0`. Three constant-fallback sites
are eligible; two dynamic-fallback sites stay unchanged.

## Agents

- frontend: implement fallback param in both stores and update 3 call sites (see detailed changes below)
- backend: skip
- broker: skip
- doc: skip
- backend-test: add Vitest tests for the new fallback param behavior in holdingsDayPnlStore and positionsDerivedStore

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

refactor(holdingsDayPnlStore,positionsDerivedStore): add fallback param to .get()/.getByRoot(); eliminate ?? 0 at call sites

## Done when

- `holdingsDayPnlStore.get(sym, fallback=null)` — fallback applies to day_pnl and chg_pct
- `positionsDerivedStore.get(sym, fallback=null)` and `getByRoot(root, fallback=null)` — fallback applies to all 6 fields
- 3 call sites updated: MarketPulse:1997, derivatives:919, derivatives:3592
- Dynamic-fallback sites (MarketPulse:3620, pulseColumns:472) unchanged
- svelte-check: 0 errors
- vitest: all pass

## Detailed changes

### 1. `frontend/src/lib/data/holdingsDayPnlStore.svelte.js`

Change `get(sym)` → `get(sym, fallback = null)`:
```javascript
get(sym, fallback = null) {
  const sym_upper = String(sym || '').toUpperCase();
  return {
    day_pnl: this.byKey[sym_upper] ?? fallback,
    chg_pct: this.chgPctByKey[sym_upper] ?? fallback,
  };
},
```

### 2. `frontend/src/lib/data/positionsDerivedStore.svelte.js`

Change `get(sym)` and `getByRoot(root)` to accept `fallback = null`, building
per-field fallback object instead of returning `_EMPTY_POS` frozen sentinel:
```javascript
get(sym, fallback = null) {
  const r = this.byKey[String(sym || '').toUpperCase()];
  if (!r) return { day_pnl: fallback, pnl: fallback, exp_pnl: fallback, extrinsic: fallback, prev_mv: fallback, chg_pct: fallback };
  return {
    day_pnl: r.day_pnl ?? fallback,
    pnl: r.pnl ?? fallback,
    exp_pnl: r.exp_pnl ?? fallback,
    extrinsic: r.extrinsic ?? fallback,
    prev_mv: r.prev_mv ?? fallback,
    chg_pct: r.chg_pct ?? fallback,
  };
},
getByRoot(root, fallback = null) {
  const r = this.byRootPositions[String(root || '').toUpperCase()];
  if (!r) return { day_pnl: fallback, pnl: fallback, exp_pnl: fallback, extrinsic: fallback, prev_mv: fallback, chg_pct: fallback };
  return {
    day_pnl: r.day_pnl ?? fallback,
    pnl: r.pnl ?? fallback,
    exp_pnl: r.exp_pnl ?? fallback,
    extrinsic: r.extrinsic ?? fallback,
    prev_mv: r.prev_mv ?? fallback,
    chg_pct: r.chg_pct ?? fallback,
  };
},
```
`_EMPTY_POS` sentinel can be removed once both methods are updated.

### 3. Call site cleanups

**`frontend/src/lib/MarketPulse.svelte:1997`**
```javascript
// Before: acc.exp_pnl += positionsDerivedStore.get(sym).exp_pnl ?? 0;
// After:  acc.exp_pnl += positionsDerivedStore.get(sym, 0).exp_pnl;
```

**`frontend/src/routes/(algo)/admin/derivatives/+page.svelte:919`**
```javascript
// Before: flash.update(`${g.underlying}:day_w`, positionsDerivedStore.getByRoot(g.underlying).day_pnl ?? 0);
// After:  flash.update(`${g.underlying}:day_w`, positionsDerivedStore.getByRoot(g.underlying, 0).day_pnl);
```

**`frontend/src/routes/(algo)/admin/derivatives/+page.svelte:3592`**
```javascript
// Before: holdingsDayPnlStore.get(String(h?.tradingsymbol || h?.symbol || '').toUpperCase()).day_pnl ?? 0,
// After:  holdingsDayPnlStore.get(String(h?.tradingsymbol || h?.symbol || '').toUpperCase(), 0).day_pnl,
```

### Sites left unchanged (dynamic fallback)
- `MarketPulse.svelte:3620` — `?? p.data?.day_pnl`
- `pulseColumns.js:472` — `?? holdingsDayPnlStore.get(sym).chg_pct`
