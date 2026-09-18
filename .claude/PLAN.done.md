# Plan: Pulse positions/holdings — chg% placement, LTP tinting, flash consistency

## Context
Four related UX issues on the Pulse page right grid (positions + holdings):
1. `day_pnl_pct` (Chg %) is far from LTP — user wants them adjacent
2. LTP has no static green/red background tint in pinned/watchlist/gainers/losers; chg% does (`mp-pnl-cell`)
3. `day_pnl_pct` column never re-renders on tick because it's missing from `refreshCells` columns
4. `left_change_pct` (chg% in left grid) never re-renders on tick for the same reason

## Agents
- frontend: All four fixes below, plus column-consistency note in PR description
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Changes

### 1. Move `day_pnl_pct` next to `ltpCol` in `mkRightColDefs`
File: `frontend/src/lib/data/pulseColumns.js` lines 596–635

Current order: `ltpCol, lots, qty_net, avg, prevCol, day_pnl, day_pnl_pct, pnl, ...`  
New order: `ltpCol, day_pnl_pct, lots, qty_net, avg, prevCol, day_pnl, pnl, ...`

Cut the `day_pnl_pct` object (lines 628–635) and paste it immediately after `ltpCol` (line 596).

### 2. LTP static background tinting — add `mp-pnl-cell` directional class
File: `frontend/src/lib/data/pulseColumns.js` — `_ltpCellClass` function (line 308)

After `cls.push(ltpDayClass(dayPct))`, append directional class + `mp-pnl-cell`:
```js
const dirBg = dayPct == null ? 'cell-flat'
  : dayPct > 0.001 ? 'cell-pos'
  : dayPct < -0.001 ? 'cell-neg'
  : 'cell-flat';
cls.push(dirBg, 'mp-pnl-cell');
```
This makes LTP background tinting consistent with chg% in all grids (left and right).
`dayPct` is already computed on line 304 — no extra data fetch needed.

### 3. Add `day_pnl_pct` + `left_change_pct` to `_scheduleFlashRefresh`
File: `frontend/src/lib/MarketPulse.svelte` lines 2298–2310

**Line 2298** — add `'day_pnl_pct'` to `cols`:
```js
const cols = ['ltp', 'sparkline', 'day_pnl', 'pnl', 'day_pnl_pct'];
```

**Lines 2300, 2302** — add `'left_change_pct'` to pinned and watchlist refresh:
```js
gridPinned.refreshCells({ columns: ['ltp', 'sparkline', 'left_change_pct'], force: true });
gridWatch.refreshCells({ columns: ['ltp', 'sparkline', 'left_change_pct'], force: true });
```

This ensures chg% column re-renders on every tick for all six grids. The `mkPnlCellClass`
`quote_symbol` fallback already handles root rows — just needed the column in the refresh list.

## Column consistency note (for your review)

After these changes, the shared column spine is:

**Positions:** `St | Sym | Spark | LTP | Chg% | Lots | Qty | Avg | P.Close | Day P&L | P&L | Exp P&L | Extrinsic | P&L% | Open | Vol | OI | Acct`

**Holdings:** `Sym | Spark | LTP | Chg% | Qty | Avg | P.Close | Day P&L | P&L | P&L% | P&L/sh | Lots* | Invested | Value | Open | Vol | OI | Acct`

The shared spine (LTP→Chg%→Qty→Avg→P.Close→Day P&L→P&L→P&L%) is identical.

One inconsistency: holdings reorders Lots to sit before `Invested` (existing logic, line 3728–3734
of MarketPulse.svelte). This is contextually reasonable (lots × avg ≈ invested), but breaks
the Lots|Qty adjacency seen in positions. Options:
- **Keep as-is** — logical grouping with Invested
- **Remove the reorder** — consistent Qty|Lots pair across both tabs (simpler code too)

Recommend removing the reorder so both tabs show `... | Qty | Lots | Avg | ...` — cleaner
and removes the splice logic. Let me know and I'll include it in this impl.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(pulse): move Chg% next to LTP, sync tinting + flash refresh across all grids

## Done when
- Positions and holdings both show LTP | Chg% adjacent
- LTP has green/red background tint matching chg% in pinned/watchlist/gainers/losers
- Chg% and LTP flash on every tick in all grids including root rows in positions
- svelte-check 0 errors
