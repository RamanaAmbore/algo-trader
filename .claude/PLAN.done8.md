# Plan: SSOT enforcement — all derived fields in store, surfaces read-only

## Context

`chg%` shows 0 in derivatives legs. Root cause is broader: **consumer surfaces (CandidateLegRow,
pulseColumns, derivatives flash effect) each carry independent fallback chains that diverge from
each other and from portfolioStore**. Pulse and Legs show different `chg%` values for the same
position because they read different broker fields when the store returns null.

**Architectural rule**: All derivation logic belongs in the store. Surfaces read the stored value
and show null if null. No consumer has its own fallback chain.

**The immediate trigger** (chg% reset to zero): SWR refresh ~30s after page load switches from
snapshot path (daily_book, correct prev_close) to live broker path. Live broker data can have
`close_price = 0` (MCX call-auction gap, stale ticker). portfolioStore Tier 2 then computes
`prev_mv = null` → `chg_pct = null`. Consumers fall back to divergent broker fields.

**All violations identified** (from audit of CandidateLegRow.svelte, pulseColumns.js, +page.svelte):

1. `portfolioStore Tier 2` — `prev_mv = null` when `prev_close = 0`, even for new intraday
   positions where `avg_cost` is a valid denominator (no prior session close exists by design).
2. `CandidateLegRow._chgPct` — 3-tier cascade: store → `c.chg_pct` → `symbolStore.day_change_pct`.
3. `CandidateLegRow.pnl` — formula `(ltp - cost) * displayQty + realised` duplicates store computation.
4. `+page.svelte flash chg` — inline formula `(ltp - prev_close) / prev_close * 100`, diverges from store.
5. `+page.svelte flash day` — calls `_candDayPnl(c)` (independent `livePositionDayPnl` computation).
6. `pulseColumns._dayPnlPctValueGetter` — falls back to `change_pct` broker field (market-data %, not day P&L %).

## Task

**Frontend changes only** — enforce SSOT: fix the store denominator, then strip all fallback chains
from consumers. Surfaces only read; store owns all logic.

## Agents

- frontend: implement all changes below
- frontend-test: add/update Vitest tests for portfolioStore prev_mv denominator fix, CandidateLegRow chg_pct SSOT, pulseColumns change_pct removal
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Changes

### 0a. `positionsDerivedStore.svelte.js` — add `get(sym)` and `getByRoot(root)` accessors

```javascript
const _EMPTY_POS = Object.freeze({
  day_pnl: null, pnl: null, exp_pnl: null,
  extrinsic: null, prev_mv: null, chg_pct: null,
});

export function get(sym) {
  return byKey[String(sym || '').toUpperCase()] ?? _EMPTY_POS;
}

export function getByRoot(root) {
  return byRootPositions[String(root || '').toUpperCase()] ?? _EMPTY_POS;
}
```

All positions lookups: `positionsDerivedStore.get(sym).field` or `positionsDerivedStore.getByRoot(root).field`.

---

### 0b. `holdingsDayPnlStore.svelte.js` — add `get(sym)` accessor

`holdingsDayPnlStore` currently exposes separate maps (`chgPctByKey`, etc.) not a unified `byKey`.
Frontend agent must either (a) unify to `byKey[sym] = { day_pnl, chg_pct }` and add `get(sym)`,
or (b) have `get(sym)` assemble the object from existing maps — whichever is least disruptive.
Do NOT change computation logic, only add the accessor.

```javascript
const _EMPTY_HOLD = Object.freeze({ day_pnl: null, chg_pct: null });

export function get(sym) {
  return byKey[String(sym || '').toUpperCase()] ?? _EMPTY_HOLD;
}
```

---

### 1. `portfolioStore.svelte.js` — Tier 2 `prev_mv` denominator (root cause of chg%=0)

**Location**: `_posTier2` block, ~line 105

```javascript
// Before
const prev_mv = p._prev_close != null && p._prev_close > 0
  ? p._prev_close * Math.abs(p._qty) : null;

// After — avg fallback for new intraday positions (oq=0, no prior session close)
const oq = Number(p?.overnight_quantity ?? 0);
const prev_mv =
  p._prev_close != null && p._prev_close > 0 ? p._prev_close * Math.abs(p._qty)
  : oq === 0 && p._avg > 0                    ? p._avg       * Math.abs(p._qty)
  : null;
```

Overnight positions with `prev_close=0` stay null — avg_cost ≠ prior close, honest unknown.

---

### 2. `CandidateLegRow.svelte` — `_chgPct` (lines 139–144)

```javascript
// Before — 3-tier broker fallback
const _chgPct = $derived.by(() => {
  const stored = positionsDerivedStore.byKey[c.symbol]?.chg_pct;
  if (stored != null) return stored;
  if (c.chg_pct != null && c.chg_pct !== 0) return c.chg_pct;
  return untrack(() => getSnapshot(c.symbol))?.day_change_pct ?? null;
});

// After
const _chgPct = $derived(positionsDerivedStore.get(c.symbol).chg_pct);
```

---

### 3. `CandidateLegRow.svelte` — `pnl` (lines 110–120)

```javascript
// Before — formula duplicates store
const pnl = $derived(
  c._residualQty != null
    ? ((ltp != null && cost != null && !_ltpFromFallback)
        ? (ltp - cost) * displayQty + Number(c.realised || 0) : null)
    : (c.pnl != null ? Number(c.pnl)
        : (ltp != null && cost != null && !_ltpFromFallback
            ? (ltp - cost) * displayQty + Number(c.realised || 0) : null))
);

// After — store first; formula only for residual positions (not in store)
const pnl = $derived.by(() => {
  if (c._residualQty == null) {
    const stored = positionsDerivedStore.get(c.symbol).pnl;
    if (stored != null) return stored;
  }
  if (ltp != null && cost != null && !_ltpFromFallback)
    return (ltp - cost) * displayQty + Number(c.realised || 0);
  return c.pnl != null ? Number(c.pnl) : null;
});
```

---

### 4. `+page.svelte` — flash chg (~line 976)

```javascript
// Before — LTP price % (wrong metric, diverges from store)
flash.update(`leg:${k}:chg`, (c.prev_close ?? 0) > 0 && c.ltp != null
  ? ((Number(c.ltp) - Number(c.prev_close)) / Number(c.prev_close)) * 100 : null);

// After
flash.update(`leg:${k}:chg`, positionsDerivedStore.get(c.symbol).chg_pct);
```

---

### 5. `+page.svelte` — flash day / `_candDayPnl` (~lines 960–981)

```javascript
// Before — independent livePositionDayPnl() call
flash.update(`leg:${k}:day`, _candDayPnl(c));

// After
flash.update(`leg:${k}:day`, positionsDerivedStore.get(c.symbol).day_pnl);
```

`_candDayPnl` function: remove if no other callers remain (frontend agent must verify).

---

### 6. `+page.svelte` — `extrinsic` prop on CandidateLegRow (~line 4557)

```javascript
// Before
extrinsic={positionsDerivedStore.byKey[String(c.symbol || '').toUpperCase()]?.extrinsic ?? null}

// After
extrinsic={positionsDerivedStore.get(c.symbol).extrinsic}
```

---

### 7. `+page.svelte` — `byRootPositions` access (~line 4703)

```javascript
// Before
const _snRow = positionsDerivedStore.byRootPositions[g.underlying];
const dayVal = _snRow?.day_pnl ?? 0;
const pnlVal = _snRow?.pnl ?? 0;
const expVal = _snRow?.exp_pnl ?? 0;

// After
const _snRow = positionsDerivedStore.getByRoot(g.underlying);
const dayVal = _snRow.day_pnl ?? 0;
const pnlVal = _snRow.pnl ?? 0;
const expVal = _snRow.exp_pnl ?? 0;
```

---

### 8. `pulseColumns.js` — `_dayPnlPctValueGetter` (lines 470–478)

```javascript
// Before — if-chain + broker change_pct fallback
const posStored = positionsDerivedStore.byKey[sym]?.chg_pct;
if (posStored != null) return posStored;
const holdStored = holdingsDayPnlStore.chgPctByKey[sym];
if (holdStored != null) return holdStored;
const cp = Number(p.data?.change_pct);
return Number.isFinite(cp) ? cp : null;

// After — ?? chain, no if statements
return positionsDerivedStore.get(sym).chg_pct ?? holdingsDayPnlStore.get(sym).chg_pct;
```

---

### 9. `pulseColumns.js` — `exp_pnl` getter (~line 730)

```javascript
// Before
return getDerivedByKey()[sym]?.exp_pnl ?? null;

// After
return positionsDerivedStore.get(sym).exp_pnl;
```

---

### 10. `pulseColumns.js` — `extrinsic` getter (~line 757)

```javascript
// Before
return getDerivedByKey()[sym]?.extrinsic ?? null;

// After
return positionsDerivedStore.get(sym).extrinsic;
```

---

### 11. `MarketPulse.svelte` — `exp_pnl` accumulator (~line 1997)

```javascript
// Before
const expPnl = positionsDerivedStore.byKey[sym]?.exp_pnl;
if (expPnl != null) acc.exp_pnl += Number(expPnl) || 0;

// After
acc.exp_pnl += positionsDerivedStore.get(sym).exp_pnl ?? 0;
```

---

### 12. `MarketPulse.svelte` — `day_pnl` per-row getter (~line 3618)

```javascript
// Before
const storeVal = positionsDerivedStore.byKey[sym]?.day_pnl;
if (storeVal != null) return storeVal;
return p.data?.day_pnl ?? null;

// After — store first, row field as fallback (row is per-position; store is per-symbol aggregate)
return positionsDerivedStore.get(sym).day_pnl ?? p.data?.day_pnl;
```

---

### Out of scope (acceptable — not store violations)

- `CandidateLegRow.ltp` — reads from `liveSnap(sym)` (symbolStore); `lg?.ltp` is legAnalytics (store-derived); acceptable.
- `pulseColumns.pnl_pct` — pure ratio math `(pnl / _cost_basis) * 100`; no store analog.
- `pulseColumns.weight_pct` — relative % requiring portfolio total; column-local math.
- `_candDayPnl` for draft legs — drafts are never in the store; local formula is the only option.
- `_mergedEv/_mergedPop/_mergedEvPct` — equity+option payoff merge not supported by backend.

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

Vitest only (`frontend/src/lib/__tests__/data/`):
- `portfolioStore.test.js`: `prev_mv` uses `avg_cost` when `oq=0` and `prev_close=0`
- `portfolioStore.test.js`: `prev_mv` null for overnight with `prev_close=0` (no spurious avg fallback)
- `positionsDerivedStore.test.js`: `get(unknown)` returns `_EMPTY_POS` with all fields null
- `positionsDerivedStore.test.js`: `getByRoot(unknown)` returns `_EMPTY_POS` with all fields null
- `holdingsDayPnlStore.test.js`: `get(unknown)` returns `_EMPTY_HOLD` with all fields null
- `pulseColumns.test.js`: chg% returns null (not `change_pct`) when both stores return null

## Commit message

fix(portfolioStore,derivatives): SSOT enforcement — get(sym) accessors on all stores, prev_mv avg fallback for intraday, strip all broker-field fallbacks

## Done when

- `positionsDerivedStore.get(sym)`, `getByRoot(root)`, and `holdingsDayPnlStore.get(sym)` all return `_EMPTY` for unknown symbols
- No consumer writes `byKey[sym]?.field` or `chgPctByKey[sym]` — every lookup uses `get(sym).field`
- `portfolioStore` Tier 2 `prev_mv` uses `avg_cost` fallback for `oq=0` intraday positions
- `CandidateLegRow._chgPct` is one line; `pnl` reads store first
- `+page.svelte` flash chg/day use `get(sym)` — no inline formulas
- `pulseColumns` all getters use `get(sym)` — no `change_pct` fallback, no `getDerivedByKey()[sym]?.`
- `MarketPulse` `exp_pnl` and `day_pnl` use `get(sym)`
- Pulse and Legs show identical `chg%` for the same position
- Vitest 0 failures; svelte-check 0 errors
