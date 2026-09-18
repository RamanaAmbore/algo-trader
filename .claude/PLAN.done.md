# Plan: Pulse & Derivatives UI — Column widths, decorations, flash

## Task
Six related UI changes across MarketPulse and Derivatives:
1. Reduce Day P&L / P&L / Exp P&L / Extrinsic column widths by 25% in Pulse positions+holdings (ag-Grid) and derivatives legs (CSS Grid)
2. Reduce `St` column width in legs grid from 38px → 28px (match positions)
3. Add alternating row background to derivatives legs grid (match positions/holdings tint)
4. Extend symbol column right-border direction indicator (green=long, red=short) to ALL grids: pinned, watchlist, winners, losers, snapshot, legs
5. Apply LTP + Chg% tick flash to derivatives legs LTP column, snapshot underlying LTP, and payoff overlay current-value indicator
6. Add `cand-row:nth-child` alternating tint to snapshot (byund-grid) if not already present

## Agents
- frontend: all changes — pulseColumns.js, MarketPulse.svelte, app.css, derivatives +page.svelte, CandidateLegRow.svelte, OptionsPayoff.svelte
- backend-test: skip
- frontend-test: vitest + svelte-check

## Critical files
- `frontend/src/lib/data/pulseColumns.js` — column widths (mkRightColDefs, mkExpPnlCol, mkExtrinsicCol)
- `frontend/src/lib/MarketPulse.svelte` — symbol right-border CSS extension for pinned/watchlist/winners/losers rows
- `frontend/src/app.css` — extend `ag-col-sym::after` rule to cover left-grid symbol column class (symColLeft → `ag-col-sym-left` or use existing class)
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — st width (38→28px), CSS Grid column minmax reduction, snapshot right-border, snapshot flash wiring
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` — LTP flash (tf-up/tf-down), chg% flash, alternating row bg
- `frontend/src/lib/OptionsPayoff.svelte` — flash pulse on current-value indicator when LTP changes

## Detailed changes

### 1. pulseColumns.js — column widths
`mkRightColDefs` (positions/holdings right grid):
- `day_pnl` (Day P&L): width 78→58, minWidth 60→45, maxWidth 96→72
- `pnl` (P&L): width 78→58, minWidth 60→45, maxWidth 96→72

`mkExpPnlCol` / `mkExtrinsicCol`:
- width 90→68 (no separate min/max on these)

### 2. Derivatives legs grid — st + column widths
`+page.svelte` `.cand-grid` grid-template-columns:
- St column: 38px → 28px
- Day P&L: `minmax(62px, max-content)` → `minmax(46px, max-content)`
- P&L: `minmax(72px, max-content)` → `minmax(54px, max-content)`
- Exp P&L: `minmax(72px, max-content)` → `minmax(54px, max-content)`
- Extrinsic: `minmax(72px, max-content)` → `minmax(54px, max-content)`

### 3. Legs alternating row background
`CandidateLegRow.svelte` (or `+page.svelte` `:global`):
```css
.cand-row:nth-child(odd):not(.cand-total-row) {
  background-color: var(--row-tint-odd-bg);
}
```

### 4. Symbol right-border direction indicator — all grids
Currently `app.css` only covers `pos-long`/`pos-short`/`row-hold-up`/`row-hold-down` row classes with `ag-col-sym` column class.

**Winners/Losers grids**: They show position rows. They use `symColLeft` not `ag-col-sym` as the symbol cell class. Add parallel `::after` rules for `symColLeft` (or whatever class `symColLeft` assigns via cellClass) with `pos-long`/`pos-short` row classes.

**Pinned/Watchlist grids**: These show market data not positions. Symbol column uses `symColLeft`. Direction is based on `change_pct` sign. In `mkLeftColDefs`, the `symColLeft` column def needs a `cellClassRules` or `getRowClass` in the grid options to add `chg-up`/`chg-down` based on `change_pct`. Then add CSS `::after` rules for those classes.

**Snapshot (byund-grid)**: The underlying column (`.byund-und`) gets a similar `position: relative; ::after` right-border decoration based on the portfolio direction for that underlying (net long=green, short=red). This requires a CSS class on each `.byund-row` based on net direction.

**Legs**: Already done — `cand-sym-acct::after` in CandidateLegRow.svelte.

### 5. LTP + Chg% flash for derivatives
**Legs grid (CandidateLegRow.svelte)**:
- Import `createTickFlash` from `$lib/data/tickFlash.svelte.js` at component level (or use a shared instance passed from parent)
- Track previous LTP and chg% via `$effect` comparing `liveSnap(sym).ltp` to stored previous value
- Apply `tf-up`/`tf-down` class to the LTP cell and chg% cell for 300ms

**Snapshot grid (byund-grid in +page.svelte)**:
- Track previous aggregate LTP/dayPnl for each underlying
- Apply `tf-up`/`tf-down` on the day P&L or LTP cells when value changes

**Payoff overlay (OptionsPayoff.svelte)**:
- Flash the current-spot indicator value or the current P&L value on the stat-overlay when LTP changes
- Apply `tf-up`/`tf-down` to the spot/payoff span using a `$derived` comparing current vs previous LTP via `liveSnap`

## Tests
- pytest: no — backend unchanged
- svelte-check: yes
- vitest: yes — update pulseColumns.test.js for new widths

## Commit message
feat(ui): column width reduction, direction borders all grids, flash for derivatives

## Done when
- Day P&L/P&L/Exp P&L/Extrinsic columns 25% narrower in Pulse positions/holdings and derivatives legs
- St column in legs = 28px
- Alternating row tint in legs matches positions/holdings rhythm
- Symbol column right-border (green/red direction bar) visible in pinned, watchlist, winners, losers, snapshot, legs
- LTP and chg% cells in legs and snapshot flash tf-up/tf-down on tick
- Payoff overlay stat flashes on LTP change
- vitest 0 failures, svelte-check 0 errors
