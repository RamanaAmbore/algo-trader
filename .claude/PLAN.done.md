# Plan: Fix agDirCellText colors + Lifetime→P&L rename + SPOT→LTP in payoff overlay

## Context
Three separate fixes:

**1. Color not applying (root cause):**
`agDirCellText` was implemented as `cellStyle` (inline style object). But `app.css` has
`.ag-theme-algo .ag-cell { color: var(--algo-slate) !important; }`. CSS `!important` in a
class rule beats inline styles without `!important`. So all directional cells show slate
regardless of value. Fix: convert `agDirCellText` to `cellClass` function using new
text-only CSS classes with `!important`.

**2. Lifetime label rename:**
`_pCols` and `_hCols` in NavBreakdown both have `headerName: 'Lifetime'` for the cumulative
P&L column. User wants both renamed to `'P&L'`.

**3. SPOT → LTP in OptionsPayoff overlay:**
`OptionsPayoff.svelte` line ~747: `<span class="ps-k">SPOT</span>` labels the underlying
price row in the payoff stats overlay. User wants it renamed to `LTP`.

## CSS specificity proof for new classes
- `.ag-theme-algo .ag-cell { color: slate !important }` — (0,2,0) + !important
- `.ag-theme-algo .dir-gain { color: green !important }` — (0,2,0) + !important
  → same specificity, last-declared wins → dir-gain wins ✓
- `.ag-theme-algo .ag-row.totals-row .ag-cell { color: amber !important }` — (0,3,0) + !important
  → higher specificity → totals row stays amber ✓

## Agents
- frontend: Three-file change:

  **File 1: `frontend/src/app.css`**
  After the existing `.ag-theme-algo .pnl-gain/.pnl-loss/.pnl-zero` block (around line 699-701),
  add three new text-only direction classes:
  ```css
  .ag-theme-algo .dir-gain { color: var(--algo-green) !important; }
  .ag-theme-algo .dir-loss { color: var(--algo-amber) !important; }
  .ag-theme-algo .dir-flat { color: var(--algo-slate) !important; }
  ```

  **File 2: `frontend/src/lib/data/algoGridUtils.js`**
  Replace the `agDirCellText` export entirely — change from cellStyle object to cellClass string:
  ```js
  /**
   * cellClass factory — direction-coloured text only (no background tint).
   * Uses dir-gain / dir-loss / dir-flat CSS classes (text-only with !important,
   * overriding the base .ag-cell rule). Color convention matches NavStrip pills:
   *   positive → --algo-green, negative → --algo-amber, zero/neutral → --algo-slate
   * @param {import('ag-grid-community').CellClassParams} p
   * @returns {string}
   */
  export const agDirCellText = (p) => {
    const v = p.value ?? 0;
    return `ag-right-aligned-cell ${v > 0 ? 'dir-gain' : v < 0 ? 'dir-loss' : 'dir-flat'}`;
  };
  ```

  **File 3: `frontend/src/lib/NavBreakdown.svelte`**
  a) For every directional column def that currently has:
       `cellClass: 'ag-right-aligned-cell', cellStyle: agDirCellText`
     Change to:
       `cellClass: agDirCellText`
     (agDirCellText now includes the right-aligned class in its return string)
  
  Affected columns (6 total):
  - `_pCols`: day_pnl, lifetime, expiry
  - `_cCols`: liveCash
  - `_hCols`: todayMtm, lifetime

  b) Remove `agDirCellText` from whatever it was used in `cellStyle:` and remove `cellStyle`
     property from those column defs.

  c) In `_pCols` — find `{ field: 'lifetime', headerName: 'Lifetime', ...}` and change to
     `headerName: 'P&L'`

  d) In `_hCols` — find `{ field: 'lifetime', headerName: 'Lifetime', ...}` and change to
     `headerName: 'P&L'`

  e) Also update `_caption` strings in the $derived if they reference "Lifetime P&L" — change
     `'Today MTM | Current Value | Lifetime P&L'` to `'Today MTM | Current Value | P&L'`
     and `'Day P&L | Lifetime P&L (Σ pnl) | Expiry P&L (lognormal projection)'` to
     `'Day P&L | P&L (Σ pnl) | Expiry P&L (lognormal projection)'`

  f) Also update the CSV export column header:
     In the P slot export: `{ header: 'Lifetime P&L', key: 'lifetimePnl', ... }` → `{ header: 'P&L', ... }`
     In the H slot export: `{ header: 'Lifetime P&L', key: 'lifetimePnl', ... }` → `{ header: 'P&L', ... }`

  **File 4: `frontend/src/lib/OptionsPayoff.svelte`**
  Line ~747: change `<span class="ps-k">SPOT</span>` to `<span class="ps-k">LTP</span>`
  This renames the underlying price label in the payoff stats overlay from "SPOT" to "LTP".
  No other changes to OptionsPayoff.svelte.

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(NavBreakdown): cellClass dir-gain/dir-loss/dir-flat for directional colors; rename Lifetime→P&L; SPOT→LTP in payoff overlay

## Done when
- NavBreakdown P/H slot directional cells show green (positive) / amber (negative) / slate (zero)
- "Lifetime" column header shows "P&L" in P slot and H slot
- OptionsPayoff stats overlay shows "LTP" instead of "SPOT"
- Totals row still amber (higher CSS specificity wins)
- svelte-check 0 errors
