# Plan: Remove equity hedge columns from snapshot grid

## Context

The snapshot grid has F&O-only columns (Day P&L, P&L, Exp P&L) and equity-inclusive
columns (H Day P&L, Day P&L Net, P&L Net, Exp P&L Net, Eq Qty). The total row includes
both, so the snapshot Day P&L total ≠ NavStrip P1 slot (which is F&O-only positions).

Operator: remove equity hedge columns entirely so the snapshot total row is F&O-only
and matches NavStrip P1. Remove all stale derived state that only served those columns.

---

## File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

### 1. Remove derived state (stale after columns gone)

- **`_hDayByRoot`** (~line 1007): drives H Day P&L column → delete
- **`_hPnlByRoot`** (~line 922): drives P&L Net / Exp P&L Net → delete
- **`_hExpByRoot`** (~line 942): drives Exp P&L Net total → delete

### 2. Remove flash updates for equity columns (~lines 1088-1091)

Delete these two lines from the underlying-groups $effect:
```js
flash.update(`${g.underlying}:day_h`,  g.day_with);
flash.update(`${g.underlying}:pnl_h`,  g.pnl_with);
```

### 3. Simplify `_byUnderlyingTotal` (~lines 1280-1300)

- Remove the holdings pass: `for (const _h of holdings) { _accumulateHoldingTotal(...) }`
- Remove `_accumulateHoldingTotal` function (~lines 1254-1270) — only served net columns
- Remove `pnl_with`, `day_with`, `legs_with`, `qty_eq` fields from accumulator object
- Keep: `pnl_without`, `day_without`, `legs_without`, `qty_fno`
- In total row, Legs display: just `Math.round(t.legs_without)` (no with/without split)

### 4. Per-row download data (~lines 4920-4965)

In the `onDownload` handler:
- Remove `const hDay = _hDayByRoot[...]` and `const hPnl = _hPnlByRoot[...]`
- Remove from row object: `h_day_pnl`, `day_pnl_net`, `pnl_net`, `exp_pnl_net`, `qty_eq`
- Change `legs: g.legs_with` → `legs: g.legs_without`
- Remove from CSV column defs: H Day P&L, Day P&L Net, P&L Net, Exp P&L Net, Eq Qty

### 5. Column header row (~lines 4988-5002)

Remove these header `<span>` cells:
- H Day P&L
- Day P&L Net
- P&L Net
- Exp P&L Net
- Eq Qty

### 6. Per-row grid cells (~lines 5037-5082)

- Remove `{@const _hDay}`, `{@const _hPnl}`, `{@const _dayNet}`, `{@const _pnlNet}`, `{@const _expNet}` local const blocks
- Remove H Day P&L `<span>`
- Remove Day P&L Net `<span>` (flash: `day_h`)
- Remove P&L Net `<span>` (flash: `pnl_h`)
- Remove Exp P&L Net `<span>`
- Remove Eq Qty `<span>` (`g.qty_eq`)
- Legs: change `g.legs_with` → `g.legs_without`, remove the `/without` conditional

### 7. Total row (~lines 5082-5117)

- Remove `{@const _hDayTotal}`, `{@const _hPnlTotal}`, `{@const _hDayNetTotal}`, `{@const _hPnlNetTotal}`, `{@const _hExpNetTotal}` local vars
- Remove H Day P&L cell
- Remove Day P&L Net cell
- Remove P&L Net cell
- Remove Exp P&L Net cell
- Remove Eq Qty cell
- Legs: `Math.round(_byUnderlyingTotal.legs_without)` only

### 8. CSS grid-template-columns (~lines 5975-5990)

Remove these five tracks:
```css
minmax(3.8rem, 0.6fr)  /* H Day P&L */
minmax(3.8rem, 0.6fr)  /* Day P&L Net */
minmax(3.8rem, 0.6fr)  /* P&L Net */
minmax(4rem,   0.6fr)  /* Exp P&L Net */
minmax(4rem,   0.6fr)  /* Eq qty */
```

---

## Agents

- frontend: make all changes above in the single derivatives page file
- backend: skip
- doc: skip
- backend-test: skip

## Tests

- svelte-check: yes — 0 errors
- vitest: yes — 968 passed

## Commit message

feat(derivatives): remove equity hedge columns from snapshot — F&O-only total syncs with NavStrip P1

## Done when

- H Day P&L, Day P&L Net, P&L Net, Exp P&L Net, Eq Qty columns gone from grid + total row
- `_hDayByRoot`, `_hPnlByRoot`, `_hExpByRoot`, `_accumulateHoldingTotal` removed
- Snapshot total Day P&L = F&O positions only (= NavStrip P1)
- svelte-check 0 errors, vitest 968 passed
