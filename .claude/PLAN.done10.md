# Plan: Fix stale day_change_val reads in derivatives, pulseUnified, PerformancePage

## Context

Four code sites read `day_change_val` directly from broker row objects instead of routing
through `holdingsDayPnlStore`, bypassing the stale-close rescue (epsilon guard +
`close <= 0` fallback) that the store applies. During the overnight BHAV-copy window
(MCX close ~00:15 IST to next session 08:00 IST) and on weekends, this produces
wrong holdings day P&L on the derivatives page rollup, the excluded-accounts counter,
the pulse unified merge, and the PerformancePage TOTAL row.

P0 (produce wrong values visible to operator):
- `derivativesMath.js:443` — `rollupByUnderlying` holdings loop
- `derivatives/+page.svelte:3590` — excluded-accounts loop

P1 (structural divergence, guard inconsistency):
- `pulseUnified.js:564` — guard `=== 0` should be `<= 0`; `close_price` fallback re-introduces the stale field we're rescuing against
- `PerformancePage.svelte:863` — `makeHoldingsTotals` sums raw `day_change_val`; `:899` — `makePositionsTotals` Chg% denominator uses raw `close_price`

## Files to change

| File | Change |
|---|---|
| `frontend/src/lib/data/derivativesMath.js` | Add `holdingsDayPnlByKey={}` param to `rollupByUnderlying`; use it in holdings loop |
| `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` | Import `holdingsDayPnlStore`; pass `.byKey` to `rollupByUnderlying`; fix excluded loop with `.get()` |
| `frontend/src/lib/data/pulseUnified.js` | Fix guard `=== 0` → `<= 0`; drop `close_price` fallback from `holdClose` |
| `frontend/src/lib/PerformancePage.svelte` | Import `holdingsDayPnlStore`; use `.total` in `makeHoldingsTotals`; fix `close_price` denominator in `makePositionsTotals` |
| `frontend/src/lib/data/pulseColumns.js` | Suppress `—` in pinned total rows for non-aggregatable columns (LTP, Chg%, Prev, Avg, Open) |
| `frontend/src/lib/NavBreakdown.svelte` | Suppress `—` in total rows (account==='TOTAL') for non-aggregatable cells |
| `frontend/src/lib/__tests__/data/derivativesMath.test.js` | Add tests for `holdingsDayPnlByKey` param (stale bypass, correct override) |
| `frontend/src/lib/__tests__/data/pulseUnified.test.js` | Add/update tests for `holdClose <= 0` guard + `close_price` fallback removal |

## Detailed changes

### 1. `derivativesMath.js` — `rollupByUnderlying`

**Add param** (signature change, backward-compat defaulting to `{}`):
```javascript
export function rollupByUnderlying({
  positions, holdings, ...,
  holdingsDayPnlByKey = {},   // ← new: caller injects store values
}) {
```

**Holdings loop (line ~443)** replace:
```javascript
const day = Number(h.day_change_val) || 0;
```
with:
```javascript
const day = holdingsDayPnlByKey[sym] ?? Number(h.day_change_val) || 0;
```

### 2. `derivatives/+page.svelte`

**Import** (add to existing imports):
```javascript
import { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js';
```

**Every `rollupByUnderlying(...)` call** — add `holdingsDayPnlByKey: holdingsDayPnlStore.byKey`:
```javascript
rollupByUnderlying({
  positions: ...,
  holdings: ...,
  holdingsDayPnlByKey: holdingsDayPnlStore.byKey,  // ← new
  ...
})
```

**Excluded-accounts loop (~line 3590)** — use `.get()` (safe null return):
```javascript
// Before:
hold_day: Number(h?.day_change_val || 0),
// After:
hold_day: holdingsDayPnlStore.get(
  String(h?.tradingsymbol || h?.symbol || '').toUpperCase()
).day_pnl ?? 0,
```

### 3. `pulseUnified.js` — `mergeHoldingRows`

**`holdClose` declaration** — remove `close_price` fallback:
```javascript
// Before:
const holdClose = Number(r.previous_close) || Number(r.close_price) || 0;
// After:
const holdClose = Number(r.previous_close) || 0;
```

**Guard (line ~564)** — fix `=== 0` to `<= 0`:
```javascript
// Before:
if (holdClose === 0 || holdClose === holdAvg) {
// After:
if (holdClose <= 0 || holdClose === holdAvg) {
```

### 4. `PerformancePage.svelte`

**Import**:
```javascript
import { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js';
```

**`makeHoldingsTotals` line ~863** replace:
```javascript
const total_day_change = sum('day_change_val');
```
with:
```javascript
const total_day_change = holdingsDayPnlStore.total ?? sum('day_change_val');
```

**`makePositionsTotals` line ~899** replace `close_price`-based denominator:
```javascript
// Before:
const total_prev_val = rows.reduce(
  (s, r) => s + Math.abs(Number(r.close_price) || 0) * Math.abs(Number(r.quantity) || 0), 0);
// After (algebraic: prev_val = cur_val − day_change, avoids stale close_price):
const total_prev_val = sum('cur_val') - total_day_change;
```

### 5. `pulseColumns.js` — suppress `—` in pinned total rows

For every column in `mkRightColDefs` whose `valueFormatter` calls a numeric formatter
(`numFmt`, `priceFmt`, `pctFmtGrid`, `aggFmtGrid`, `aggCompact`) AND which the
total row does NOT populate (LTP, Chg%, Prev Close, Avg Price, Open, Vol, OI,
Delta, Theta, Margin%), wrap the formatter:
```javascript
// Before:
valueFormatter: p => numFmt(p.value)
// After:
valueFormatter: p => (p.node?.rowPinned && p.value == null) ? '' : numFmt(p.value)
```
Apply the same guard to any formatter that currently returns `'—'` for null
(e.g., `value == null ? '—' : ...`).

### 6. `NavBreakdown.svelte` — suppress `—` in total rows

In the column definitions that wrap `_fmt()`, add a total-row guard:
```javascript
// Before:
valueFormatter: p => _fmt(p.value)
// After:
valueFormatter: p => (p.data?.account === 'TOTAL' && p.value == null) ? '' : _fmt(p.value)
```
Apply only to columns that are non-aggregatable for a total row (e.g., Chg% on individual
accounts that the TOTAL row leaves null). Columns that DO aggregate (Day P&L, P&L, Value)
keep the existing `_fmt()` and will show their values.

## Agents

- frontend: implement all 6 source file changes above
- backend-test: add vitest tests to `derivativesMath.test.js` (holdingsDayPnlByKey override + stale bypass) and `pulseUnified.test.js` (guard `<= 0` + no close_price fallback)

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(derivativesMath,pulseUnified,PerformancePage,pulseColumns,NavBreakdown): route holdings day P&L through store; fix stale close_price reads; suppress — in total rows

## Done when

- `rollupByUnderlying` accepts `holdingsDayPnlByKey` and uses store values
- excluded-accounts loop uses `holdingsDayPnlStore.get(sym).day_pnl ?? 0`
- `pulseUnified` guard is `<= 0`; `close_price` fallback removed
- `PerformancePage` TOTAL row uses `holdingsDayPnlStore.total`; Chg% denominator uses `cur_val - day_change`
- Pulse pinned total rows show `''` (not `—`) for non-aggregatable columns
- NavBreakdown total rows show `''` (not `—`) for null non-aggregatable cells
- svelte-check: 0 errors
- vitest: all pass
