# Plan: Remove Day %/P&L % background tint + remove TOTAL row flash everywhere

## Context

Two related flash/animation issues in MarketPulse grids:

**Issue 1 — Percentage columns incorrectly flash:**
`day_pnl_pct` ("Day %") and `pnl_pct` ("P&L %") have `mp-pnl-cell` in their `cellClass`,
giving them a persistent green/red background tint. When poll data arrives and ag-Grid
re-evaluates cellClass, the colour change looks like a flash. Percentage columns should show
directional text colour only — no background, no flash.

**Issue 2 — TOTAL pinned-bottom row flashes (should not):**
The main positions/holdings grids in MarketPulse call `_mpFlash.update('TOTAL:day_pnl', ...)`
and `_mpFlash.update('TOTAL:pnl', ...)` on every poll, causing the TOTAL row to animate with
`tf-up/tf-down`. Operator wants TOTAL rows to NOT flash on any grid.
PerformancePage already excludes TOTAL rows from flash by design (line 363: explicit guard).
MarketPulse summary grids never had TOTAL flash — only the two main grids need cleanup.

**LTP column:** flash on ≥0.1% tick change via `ltp-flash-up/down` — correct, no change.

## Task

### Fix 1 & 2 — Remove `mp-pnl-cell` from percentage columns in right grid (`pulseColumns.js`)

`pulseColumns.js:530` — `day_pnl_pct`:
```js
// BEFORE: cellClass: (p) => `${RA} ${dirCls(p.value)} mp-pnl-cell`,
// AFTER:  cellClass: (p) => `${RA} ${dirCls(p.value)}`,
```
`pulseColumns.js:541` — `pnl_pct`:
```js
// BEFORE: cellClass: (p) => `${RA} ${dirCls(p.value)} mp-pnl-cell`,
// AFTER:  cellClass: (p) => `${RA} ${dirCls(p.value)}`,
```

### Fix 3–5 — Summary grids: use `dirCellClass` for percentage columns

`mkPosSummaryCols` (line 589): add `dirCellClass` to options; use it for `day_change_percentage` (line 596).
`mkHoldSummaryCols` (line 617): add `dirCellClass` to options; use it for `day_change_percentage` (line 624) and `pnl_percentage` (line 630).
`MarketPulse.svelte` lines 3685 + 3701: pass `dirCellClass` to both factory calls.
`dirCellClass` is already at `MarketPulse.svelte:3521`: `const dirCellClass = (p) => \`${RA} ${dirCls(p.value)}\``

**PerformancePage.svelte** — clean already (`pnlCls` uses `pnl-loss`/`pnl-gain`/`pnl-zero`). No change.

### Fix 6 — Remove TOTAL row flash updates from MarketPulse main grids

`MarketPulse.svelte` — delete the four TOTAL flash update lines:
```js
// DELETE these four lines (two in positions block ~2122-2123, two in holdings block ~2152-2153):
if (pTotal.day_pnl != null) _mpFlash.update('TOTAL:day_pnl', Number(pTotal.day_pnl));
if (pTotal.pnl     != null) _mpFlash.update('TOTAL:pnl',     Number(pTotal.pnl));
// ... and the matching hTotal lines
```

### Fix 7 — Remove dead `_isTotal` flash branch from `mkPnlCellClass` (`pulseColumns.js`)

With no `TOTAL:*` keys ever set in `_mpFlash`, the `_isTotal` branch in `mkPnlCellClass`
(lines 54-58) is dead code. Simplify:
```js
// DELETE the _isTotal branch:
if (p.data?._isTotal) {
  if (!field) return base;
  const fc = getMpFlash().classOf(`TOTAL:${field}`);
  return fc ? `${base} ${fc}` : base;
}
```
After deletion, TOTAL rows fall through to the normal `base` path — no flash, correct directional tint only.

## Agents

- backend: skip
- frontend: Apply all 7 fixes in `frontend/src/lib/data/pulseColumns.js` and
  `frontend/src/lib/MarketPulse.svelte`:
  1. Lines 530, 541 in pulseColumns.js — remove `mp-pnl-cell` from day_pnl_pct + pnl_pct.
  2. mkPosSummaryCols + mkHoldSummaryCols — add dirCellClass param, use for Day %/P&L %.
  3. MarketPulse.svelte lines 3685, 3701 — pass dirCellClass to both factory calls.
  4. MarketPulse.svelte ~2122-2123 + ~2152-2153 — delete TOTAL _mpFlash.update calls.
  5. pulseColumns.js lines 54-58 — delete the _isTotal branch from mkPnlCellClass.
  Write Vitest tests in `frontend/src/lib/__tests__/` covering:
  - `day_pnl_pct` and `pnl_pct` cellClass with positive value → no `mp-pnl-cell`, has `cell-pos`
  - `mkPosSummaryCols`/`mkHoldSummaryCols` Day % column uses dirCellClass (no `mp-pnl-cell`)
  - `mkPnlCellClass` with `_isTotal=true` row returns only `base` (no `TOTAL:*` flash class)
  For every file you change, write or update at least one test.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(ui): remove mp-pnl-cell tint from Day%/P&L% columns + remove TOTAL row flash from all grids

## Done when

- `day_pnl_pct` and `pnl_pct` cellClass contain no `mp-pnl-cell`
- Summary grid Day % and P&L % columns use `dirCellClass` (no background tint)
- No `_mpFlash.update('TOTAL:...')` calls remain in MarketPulse.svelte
- `mkPnlCellClass` has no `_isTotal` branch
- Vitest tests pass covering all three assertions above
- svelte-check 0 errors
