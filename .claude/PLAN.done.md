# Plan: Pulse page — flash fixes + column visibility + right-align + bg color

## Context
Multiple UX issues on the MarketPulse page found together:
1. LTP and Chg% cells don't flash for pinned, watchlist, gainers, losers, and holdings
2. Root spot/futures rows in positions don't flash for LTP/chg% (option rows do)
3. Holdings shows Exp P&L and Extrinsic columns (derivatives-only, meaningless for holdings)
4. Positions shows P&L/sh, Invested (inv_val), Value (cur_val) — clutter for a live view
5. Exp P&L and Extrinsic lack right-alignment (missing `ag-right-aligned-cell`)
6. Chg%, P&L%, Exp P&L lack background color tinting — P&L has it (`mp-pnl-cell`) but these don't

## Root causes

### Flash gaps (item 1 & 2)

| Tab / row | LTP flash | Chg% flash |
|---|---|---|
| pinned | `_scheduleFlashRefresh` skips `gridPinned` → LTP refresh only via slow `_liveLtpSnap` 4Hz path, up to 500ms idle delay, too late for 300ms clearance | poll-diff flash IS wired (`_mpFlash.update` + `refreshCells(['left_change_pct'])`) |
| watchlist | Same as pinned | Same |
| gainers | In `_scheduleFlashRefresh` ✓ | `$effect` (line 2195-2196) only calls `setGridOption` — NO `_mpFlash.update()` or `refreshCells` |
| losers | In `_scheduleFlashRefresh` ✓ | Same as gainers |
| holdings | In `_scheduleFlashRefresh` ✓ | `_mpFlash.update('${sym}:day_pnl_pct', ...)` silently skips when holdings API rows don't carry `day_pnl_pct` field; use `r.day_pnl_pct ?? r.change_pct` fallback |
| positions root/futures | In `_scheduleFlashRefresh` — investigate if tickBus key (e.g. `CRUDEOIL26OCTFUT`) matches the row's `tradingsymbol`; also check if futures rows are registered in symbolStore and receiving SSE ticks | Same investigation needed |

### Background color (item 6)
`mp-pnl-cell` class + `cell-pos/neg` = green/red background tinting. P&L columns use
`pnlCellClass(p, field)` which adds it. `day_pnl_pct`, `pnl_pct`, `exp_pnl`, `extrinsic`
only use `dirCls` (text color only) — missing `mp-pnl-cell`.

### Column visibility (items 3 & 4)
`rightColDefs` is shared. Holdings filters only `pos_state`. Positions uses the array
directly. Need to filter at call site.

### Right alignment (item 5)
`mkExpPnlCol` / `mkExtrinsicCol` don't accept `RA` or `numericHdr` params — `cellClass`
uses only `dirCls(p.value)`, dropping `ag-right-aligned-cell`.

## Task

### 1. `_scheduleFlashRefresh` — add pinned + watchlist (MarketPulse.svelte ~line 2271)
```js
if (gridPinnedReady && gridPinned && topTab === 'pinned')
  try { gridPinned.refreshCells({ columns: ['ltp', 'sparkline'], force: true }); } catch (_) {}
if (gridWatchReady && gridWatch && typeof topTab === 'number')
  try { gridWatch.refreshCells({ columns: ['ltp', 'sparkline'], force: true }); } catch (_) {}
```

### 2. `mkLeftColDefs` — add `mp-pnl-cell` to chg% base class (pulseColumns.js ~line 475)
Pinned/watchlist/gainers/losers `left_change_pct` currently uses `dirCellClass(p)` as
base (text color only). Change to add persistent background tinting — same as holdings:
```js
const base = `${dirCellClass(p)} mp-pnl-cell`;
```
Apply to both the flash-wired branch and the fallback branch.

### 3. Gainers/losers $effects — add flash update + refreshCells
Replace the one-liner `$effect` for gridWin (line 2195) and gridLose (line 2197) with the
full pattern (same as pinned/watchlist effects):
```js
$effect(() => { if (gridWinReady && gridWin) {
  const rows = winRows;
  untrack(() => {
    for (const r of rows) {
      const sym = r.tradingsymbol;
      if (!sym || r._isTotal) continue;
      if (r.change_pct != null) _mpFlash.update(`${sym}:change_pct`, Number(r.change_pct));
    }
    gridWin.setGridOption('rowData', rows);
    try { gridWin.refreshCells({ columns: ['left_change_pct'], force: true }); } catch (_) {}
    setTimeout(() => { try { gridWin.refreshCells({ columns: ['left_change_pct'], force: true }); } catch (_) {} }, 400);
  });
} });
// identical pattern for gridLose / loseRows
```

### 3. Holdings chg% flash — use `r.day_pnl_pct ?? r.change_pct` fallback (MarketPulse.svelte ~line 2184)
```js
const dpPct = r.day_pnl_pct ?? r.change_pct ?? null;
if (dpPct != null) _mpFlash.update(`${sym}:day_pnl_pct`, Number(dpPct));
```

### 4. Positions root/futures flash — investigate
Agent should grep for how `quote_symbol` vs `tradingsymbol` is set on futures rows in
positions data. Check if the `_ltpFlashUp/Down` key lookup uses `tradingsymbol.toUpperCase()`
(see `mkPnlCellClass` line 93) while tickBus fires with `quote_symbol`. If so, the
`pnlCellClass` LTP-cascade check needs to try `quote_symbol` too — same pattern as
`mkResolveCellLtp` (lines 132-136) which already handles this for the LTP value.
Fix: in `mkPnlCellClass`, also check `getLtpFlashUp().has(p.data?.quote_symbol?.toUpperCase())`.

### 5. Background color — add `mp-pnl-cell` to chg%, P&L%, Exp P&L, Extrinsic (pulseColumns.js)

**`day_pnl_pct` (line 627)**: change cellClass to `pnlCellClass(p, 'day_pnl_pct')` — `pnlCellClass` is already in scope via `mkRightColDefs` params (line 564); this adds `mp-pnl-cell` AND the flash cascade.

**`pnl_pct` (line 647)**: change cellClass to `(p) => \`${RA} ${dirCls(p.value)} mp-pnl-cell\`` (no flash needed here).

**`mkExpPnlCol` and `mkExtrinsicCol`**: update signature to accept `{ RA, numericHdr }`:
```js
export function mkExpPnlCol(getDerivedByKey, { RA = 'ag-right-aligned-cell', numericHdr = '' } = {}) {
  return { headerClass: numericHdr, cellClass: p => `${RA} ${dirCls(p.value)} mp-pnl-cell`, ... }
}
```
Update call site (line 646): `mkExpPnlCol(getDerivedByKey, { RA, numericHdr })`, same for extrinsic.

### 6. Column visibility — filter at call site (MarketPulse.svelte ~line 3697)

Holdings (extend existing filter):
```js
const holdingsColDefs = rightColDefs.filter(c =>
  !['pos_state', 'exp_pnl', 'extrinsic'].includes(c.colId)
);
```

Positions (create new filtered var, replace direct `rightColDefs` usage for `gridPositions`):
```js
const positionsColDefs = rightColDefs.filter(c =>
  !['pnl_per_share', 'inv_val', 'cur_val'].includes(c.colId)
);
```

## Agents
- frontend: all changes — `MarketPulse.svelte` (items 1–4, 6) and `pulseColumns.js` (items 4–5). Read exact line numbers before editing.
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(pulse): flash for pinned/watchlist/gainers/losers/holdings; bg tint for chg%/p&l%/exp-pnl; hide exp-pnl/extrinsic from holdings, pnl-per-share/inv/cur-val from positions; right-align exp-pnl/extrinsic

## Done when
- LTP + chg% flash for pinned, watchlist, gainers, losers (via `_scheduleFlashRefresh` + `_mpFlash.update`)
- Holdings chg% flash fires using `day_pnl_pct ?? change_pct` fallback
- Root/futures rows in positions flash LTP via `quote_symbol` fallback in `mkPnlCellClass`
- `day_pnl_pct`, `pnl_pct`, `exp_pnl`, `extrinsic` show green/red background tinting
- Exp P&L and Extrinsic are right-aligned with `ag-right-aligned-cell` + `numericHdr`
- Holdings tab: no Exp P&L, no Extrinsic
- Positions tab: no P&L/sh, no Invested, no Value
- svelte-check: 0 errors
