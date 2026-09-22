# Plan: NavStrip popups + pulse grid visual fixes

## Task
Six fixes identified during the 2026-09-22 planning session:
1. Add header (title + close button) to NavBreakdown popup windows.
2. Replace BrokerHealthBadge hand-rolled CSS grid with ag-Grid.
3. Fix sparkline column right-border appearing thinner in gainers/losers vs pinned.
4. Fix horizontal strips visible between rows on mobile in all ag-Grid surfaces.
5. Unify account column treatment (border stripe + amber bg) across all grids.
6. Unify symbol column direction-bar decoration across all grids.

## Fixes

### Fix 1 — NavBreakdown popup: add canonical header
**Files:** `frontend/src/lib/PositionStrip.svelte`, `frontend/src/lib/NavBreakdown.svelte`

The `ps-breakdown-panel` currently has only a floating `✕` button (position:absolute, no header bar). Add a proper header row using the `canonical-modal-header` pattern already used by BrokerHealthBadge and ChartModal.

- Map `activeSlot` → title: `P` → "Positions P&L", `M` → "Margin", `C` → "Cash", `H` → "Holdings"
- Move the close button into the header row (remove `position:absolute` from `ps-breakdown-close`)
- Pass `activeSlot` into `NavBreakdown` (already done) and emit it as a header inside the panel

### Fix 2 — BrokerHealthBadge: replace CSS grid with ag-Grid
**File:** `frontend/src/lib/BrokerHealthBadge.svelte`

Current body uses `.bh-grid` / `.bh-row` / `.bh-headrow` — hand-rolled CSS grid rows with manual column alignment. Replace with `createGrid` + `ag-theme-quartz ag-theme-algo`, matching the NavBreakdown pattern.

Columns: Status dot | Account | Broker | State | Reason | Last Good  
Use `domLayout: 'autoHeight'`, `rowHeight: 26`, same `mkBaseGridOpts` base as other algo grids.  
Destroy grid on `onDestroy` (component already has cleanup pattern for stores).

### Fix 3 — Sparkline right-border thin in gainers/losers
**File:** `frontend/src/app.css` (line ~686)

**Root cause:** In pinned, the LTP column cell has `box-shadow: inset 1px 0 0 0 <color>` (green/red/slate) from `ltp-vs-prev-*` classes. This shadow is adjacent to sparkline's `border-right: 1px amber`, making the divider appear ~2px visually. In gainers/losers, `app.css:674–688` zeroes all LTP box-shadows to `none !important` (to remove colored LTP heat), leaving only the 1px sparkline border → appears thinner.

**Fix:** Change the `box-shadow: none !important` in the `.mp-bucket-winners … .mp-bucket-losers … { }` rule to `box-shadow: inset 1px 0 0 0 rgba(126,151,184,0.40) !important`. This replaces the colored green/red shadow with a neutral slate separator — removes the semantic color meaning (the original complaint) while matching the visual weight of pinned.

### Fix 4 — Mobile horizontal row strips on all ag-Grid surfaces
**Root cause:** `MarketPulse.svelte:4573–4575` applies `:global(.ag-theme-algo .ag-row) { min-height: 36px !important }` inside a `@media (max-width: 720px)` block. Because it is `:global`, it hits **every** ag-theme-algo grid in the app — not just Pulse. ag-Grid still positions rows at the JS `rowHeight` interval. The mismatch creates an overlap zone where adjacent semi-transparent row backgrounds compound:
- Pulse gainers/losers: `rgba(green/red, 0.06)` compounds to ~12% → visible tinted strip
- All other grids: `ag-row-odd` at `rgba(13,22,42,0.30)` → at even↔odd boundary the odd row's 30% dark bleeds into the even row's transparent area → visible dark strip

**All affected grids:**

| Grid(s) | File | JS rowHeight | Overlap |
|---|---|---|---|
| Pinned, Watch, Winners, Losers, Positions, Holdings, Summary ×2, Funds | MarketPulse.svelte | 28 | 8px |
| NavBreakdown P/M/C/H | NavBreakdown.svelte | 26 (mkBaseGridOpts) | 10px |
| Dashboard W/L mini ×2 | dashboard/+page.svelte | 26 (mkBaseGridOpts) | 10px |
| NAV, Funds, PositionsSummary, PositionsAll, HoldingsSummary, HoldingsAll | PerformancePage.svelte | 28 (CSS var, no JS override) | 8px |

**Files to change:**
- `frontend/src/lib/MarketPulse.svelte` — delete the `:global` `min-height` rule (line 4573–4575); change `makeBucketGrid rowHeight: 28` → `_isMobile ? 36 : 28`
- `frontend/src/lib/data/algoGridUtils.js` — change `mkBaseGridOpts rowHeight: 26` → `_isMobile ? 36 : 26` (covers NavBreakdown + Dashboard mini grids)
- `frontend/src/lib/PerformancePage.svelte` — add `rowHeight: _isMobile ? 36 : 28` to `makeGrid`

```js
const _isMobile = typeof window !== 'undefined' && window.innerWidth <= 720;
```

### Fix 5 — Account column stripe consistency
**Files:** `frontend/src/lib/NavBreakdown.svelte`, `frontend/src/lib/data/pulseColumns.js`

**Canonical pattern** (PerformancePage): `cellClass: '... ag-col-acct'` + `cellStyle` injecting `--acct-stripe` CSS custom property. App.css rule `.ag-col-acct { border-left: 3px solid var(--acct-stripe, transparent) }` draws the colored left stripe.

**NavBreakdown bug (lines 357–369):** Account column in all 4 slot colDefs has `cellClass: 'ag-col-fill ag-col-acct'` but NO `cellStyle` → `--acct-stripe` is never set → border always transparent. Fix: add `cellStyle` that injects the DJB2-hashed account color, same pattern as PerformancePage's `acctCellStyle`. Define `acctColor()` and `ACCT_PALETTE` locally in `NavBreakdown.svelte` (copy from PerformancePage lines 483–491 + ACCT_PALETTE constant).

**MarketPulse trailing account (`mkAcctColTrailing`, pulseColumns.js ~line 383):** Currently `cellClass: 'mp-acct-cell'` + `cellStyle: p => ({ color })` (text-foreground color injection). Change to match canonical:
- `cellClass: 'mp-acct-cell ag-col-acct'` (add `ag-col-acct` for left-border)
- `cellStyle: p => color ? { '--acct-stripe': color } : { '--acct-stripe': 'transparent' }` (inject stripe not text color)
- `_acctColor` is already pre-computed per row in MarketPulse — reuse it. Keep monospace bold font from `.mp-acct-cell`.

### Fix 6 — Symbol column direction-bar consistency
**Files:** `frontend/src/app.css` (~lines 849–866), `frontend/src/lib/data/pulseColumns.js` (`mkSymColRight`), `frontend/src/routes/(algo)/dashboard/+page.svelte`

**Root cause:** Direction `::after` right-bar currently only works for:
- `ag-col-sym-left` cells with `chg-up`/`chg-down` classes (MarketPulse left-grid via cellClassRules)
- `ag-col-sym` cells in rows with row-level `pos-long`/`pos-short`/`row-hold-up`/`row-hold-down` classes (positions/holdings)

Missing: `ag-col-sym` lacks `position: relative`, so the absolute `::after` bars for row-level classes don't render. Also `mkSymColRight` (gainers/losers right-grid) has no direction encoding at all. Dashboard sym columns also have no direction encoding.

**Fixes:**

1. **`app.css`** — add `position: relative` to `.ag-theme-algo .ag-col-sym` so the existing row-level `::after` rules (pos-long/pos-short/row-hold-up/row-hold-down) render on ALL symbol cells, not just `ag-col-sym-left`. Also add cell-level `::after` rules for `.ag-col-sym.chg-up` and `.ag-col-sym.chg-down` matching the `ag-col-sym-left` equivalents (lines 849–866) — this activates cell-level direction bars on right-grid and dashboard.

2. **`pulseColumns.js` `mkSymColRight`** — add `cellClassRules: { 'chg-up': p => (p.data?.change_pct ?? 0) > 0, 'chg-down': p => (p.data?.change_pct ?? 0) < 0 }` — identical to `mkSymColLeft`. This drives the direction bar on gainers/losers symbol cells.

3. **`dashboard/+page.svelte` Winners/Losers sym column** — add `cellClassRules: { 'chg-up': p => (p.data?.change_pct ?? 0) > 0, 'chg-down': p => (p.data?.change_pct ?? 0) < 0 }` to the `{ field: 'symbol', ... }` column definition.

4. **`app.css` F&O indicator** — replace `row-hold-fno .ag-col-sym { box-shadow: inset 2px 0 0 0 #4ade80 }` (LEFT inset) with an `::after` right-bar using amber `rgba(251,191,36,0.85)` to use consistent right-edge decoration technique and distinguish F&O type (amber) from direction (green/red).

## Agents
- frontend: Implement all six fixes (all are frontend-only changes)
- frontend-test: Update/add Playwright specs covering: NavBreakdown header visible, connection chip opens ag-Grid modal, sparkline border visual consistency note, mobile viewport row height, account stripe visible in NavBreakdown, symbol direction bars visible across grids

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(pulse+navstrip): popup headers, connection chip ag-grid, sparkline border parity, mobile row-strip, account+symbol column consistency

## Done when
- NavBreakdown popup shows slot-titled header with close button matching canonical-modal-header pattern
- BrokerHealthBadge body is an ag-Grid (columns: status, account, broker, state, reason, last good)
- Sparkline right border visually matches thickness between pinned and gainers/losers
- No horizontal row strips visible between rows on mobile (<720px) in any ag-Grid surface in the app
- NavBreakdown account columns show 3px colored left-border stripe (same as PerformancePage)
- MarketPulse trailing account column shows 3px colored left-border stripe (replacing text-color treatment)
- Symbol cells across all grids (gainers/losers, dashboard W/L, positions, holdings) show 2px right-edge direction bar matching market direction (green up / red down / amber F&O type)
- svelte-check passes with 0 errors
