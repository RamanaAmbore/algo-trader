# Plan: Fix three snapshot grid defects — Day P&L zero, EV per-row, Day% flash

## Context

Three distinct defects in the derivatives snapshot grid, all in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` and its helpers:

1. **Day P&L zero for CRUDEOIL, GOLDM (and total row)** — root cause: `buildPositionRowFromBroker` in `pageLoad.js` does NOT copy `prev_settlement_pnl` from the raw broker row to the normalized position. `_dayPnlForLeg(c)` calls `baseDayPnlForPosition(c)` as fallback when `prev_close = 0` (MCX stale). That function checks `c.prev_settlement_pnl` first — but it's missing from the normalized row → falls to `day_change_val` (0 for MCX) → Case 4 → returns 0. NavStrip P1 uses `positionsDayPnlStore` which passes raw broker rows (which DO have `prev_settlement_pnl`) to `baseDayPnlForPosition`, so it computes correctly.

2. **EV only shows for active underlying** — line 4904-4909: `_mergedEv` (strategy-level probabilistic EV from backend) only exists for the selected underlying's active strategy call. Non-active rows hard-code '—'. `_expVal` (`_expPnlByRootMap[g.underlying]` = deterministic expiry P&L at current spot) is already computed per-row but used only in the Exp P&L column (line 4897). The EV column should fall back to `_expVal` for non-active rows so every row shows a meaningful value.

3. **Day% flashing** — line 1022-1023: `flash.update(`${root}:pct`, q?.day_pct)` fires whenever underlying quotes update (same cadence as LTP). Line 4893 applies `flash.classOf(`${g.underlying}:pct`)` to the Day% span. Day% is derived from LTP — user wants only LTP to flash.

---

## File: `frontend/src/lib/derivatives/pageLoad.js`

### Fix 1a — add `prev_settlement_pnl` to `buildPositionRowFromBroker` (line 61–85)

```js
// After the existing day_sell_value line, add:
prev_settlement_pnl: r?.prev_settlement_pnl != null ? Number(r.prev_settlement_pnl) : null,
```

No other changes to this function. The normalized position now carries `prev_settlement_pnl` so `baseDayPnlForPosition(c)` uses the authoritative `pnl − prev_settlement_pnl` formula instead of Case 4.

---

## File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

### Fix 1b — update JSDoc type annotation (~line 3395)

Add `prev_settlement_pnl?:number|null` to the type comment on `positions`.

### Fix 2 — EV column per row (~line 4904-4909)

Change the EV cell from:
```html
{selectedUnderlying === g.underlying && _mergedEv != null
  ? aggCompact(_mergedEv) : '—'}
```
to:
```html
{selectedUnderlying === g.underlying && _mergedEv != null
  ? aggCompact(_mergedEv)
  : _expVal !== 0 ? aggCompact(_expVal) : '—'}
```

Active row: shows probabilistic EV (`_mergedEv` from strategy analytics).  
Other rows: shows deterministic expiry P&L at current spot (`_expVal`), same computation as the Exp P&L column.

### Fix 3 — stop Day% from flashing (~lines 1022-1023 and 4893)

Remove `flash.update(`${root}:pct`, q?.day_pct)` from the underlying-quotes `$effect`.  
Remove `{flash.classOf(`${g.underlying}:pct`)}` from the Day% span.

---

## File: `frontend/src/lib/__tests__/data/pageLoad_expired.test.js`

Add one test case to the existing `buildPositionRowFromBroker` describe block (~line 403):
- Verify that `prev_settlement_pnl` is copied when present on the raw broker row
- Verify it is `null` when absent

---

## Agents

- frontend: make all changes above (pageLoad.js + derivatives/+page.svelte)
- backend-test: add Vitest test for prev_settlement_pnl in pageLoad_expired.test.js

## Tests

- svelte-check: yes — 0 errors
- vitest: yes — 968+ passed

## Commit message

fix(derivatives): copy prev_settlement_pnl to normalized position — MCX Day P&L zero fixed; EV per-row; stop Day% flash

## Done when

- CRUDEOIL, GOLDM Day P&L non-zero in snapshot, matches NavStrip P1 total
- EV column shows expiry P&L for non-active rows instead of '—'
- Day% cell no longer flashes on tick; only LTP cell flashes
- svelte-check 0 errors, vitest passed
