# Plan: NAV/Capital/Equity grid color scheme — green/amber/neutral, CSS variable consistency

## Context
NavBreakdown ag-Grid cells use `agDirCellText` from algoGridUtils.js. Currently:
- Positive → `--algo-green` ✓
- Negative → `--algo-red` ✗ (should be amber — red signals danger; amber signals caution, matching NavStrip pill convention)
- Zero → `--algo-dim` (#94a3b8) ✗ (should be `--algo-slate` #c8d8f0 — same as all other neutral/non-directional cells)

The zero/dim gap is what creates the "off-white vs bright white" inconsistency the operator sees: zero directional cells are #94a3b8 while neutral non-directional cells are #c8d8f0. Making zero use `--algo-slate` unifies them.

Additionally, the totals-row color in app.css is hardcoded `#fbbf24` — should be `var(--algo-amber)` for variable reusability.

NavBreakdown is the only consumer of `agDirCellText` (NavStrip popup windows share the same component), so the fix propagates automatically.

## Task
Two-file change:
1. `agDirCellText` in algoGridUtils.js — change negative to `--algo-amber`, zero to `--algo-slate`
2. `app.css` totals-row color — `#fbbf24` → `var(--algo-amber)`

## Color scheme after fix

| Value | Color variable | Hex | Matches NavStrip |
|---|---|---|---|
| Positive | `--algo-green` | `#4ade80` | `.ps-pos` ✓ |
| Negative | `--algo-amber` | `#fbbf24` | amber pill convention ✓ |
| Zero/neutral | `--algo-slate` | `#c8d8f0` | `.ps-flat` ✓ |
| Non-directional | `--algo-slate` (CSS default) | `#c8d8f0` | consistent ✓ |

`agDirCell` (used in positions/legs/snapshot grids, with background tints) is NOT touched — red loss is correct there.

## Agents
- frontend: Two-file change:

  **File 1: `frontend/src/lib/data/algoGridUtils.js`** — update `agDirCellText`:
  ```js
  export const agDirCellText = (p) => {
    const v = p.value ?? 0;
    return {
      color: v > 0 ? 'var(--algo-green)'
           : v < 0 ? 'var(--algo-amber)'
           : 'var(--algo-slate)',
    };
  };
  ```
  Also update the JSDoc comment above it to note amber for negative + slate for zero.

  **File 2: `frontend/src/app.css`** — find the `.ag-theme-algo .ag-row.totals-row .ag-cell` rule and change `color: #fbbf24 !important` to `color: var(--algo-amber) !important`. This is the only hardcoded color in the ag-theme-algo section that has a matching variable. Do NOT change any other lines.

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
fix(NavBreakdown): apply green/amber/slate color scheme — amber for negative, slate for neutral, CSS variable for totals-row

## Done when
- NavBreakdown directional P&L cells: positive = green, negative = amber, zero = slate
- NavStrip popup windows: same (NavBreakdown component shared)
- Totals row: still amber, now via `var(--algo-amber)` not hardcoded `#fbbf24`
- Non-directional cells (margin, value): unchanged slate
- `agDirCell` (positions/legs grids): unchanged (red loss stays)
- svelte-check 0 errors
