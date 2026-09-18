# Plan: Derivatives UI polish — chg% separator, legs chg% column, flash fix, totals tint, payoff labels

## Context
Several UI consistency and correctness gaps in the derivatives and Pulse surfaces:
1. The chg%/LOTS separator visible in positions is missing from pinned, watchlist, winners, losers, holdings, and the byund snapshot grid.
2. The legs CSS Grid has no chg% column — the value is computed but never displayed; the flash class was being applied to the LTP span as a fallback.
3. Leg chg% flash only fires on poll cycles (30s), not on SSE ticks — because the tickBus subscription (lines 1699–1704 in +page.svelte) updates `leg:${k}:ltp` but not `leg:${k}:chg`.
4. The legs totals row (`cand-row-total`) has a green tint on positive P&L cells from `:global(.cand-pnl.cell-pos)` in CandidateLegRow.svelte — not suppressed by the amber container override.
5. Payoff overlay legend labels need renaming: "P&L" → "Day P&L", "Exp P&L" → "Exp Val".

## Task
Five targeted changes across three files.

## Agents
- frontend: All five changes below
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent task

### 1. chg% separator — left grids (pinned/watchlist/winners/losers)
File: `frontend/src/lib/data/pulseColumns.js`
- In `mkLeftColDefs()`, the `left_change_pct` column (line ~451) — add `'chg-right-sep'` to its `cellClass` array alongside the existing `changePctCellClass` result.
  Change: `cellClass: changePctCellClass` → `cellClass: (p) => [changePctCellClass(p), 'chg-right-sep'].filter(Boolean).join(' ')`

File: `frontend/src/app.css`
- Add CSS for the right-border separator (mirror the existing `lots-left-sep` style with inset on the right):
  ```css
  .ag-theme-algo .ag-cell.chg-right-sep {
    box-shadow: inset -1px 0 0 0 rgba(126,151,184,0.40);
  }
  .ag-theme-ramboq .ag-cell.chg-right-sep {
    box-shadow: inset -1px 0 0 0 rgba(112,99,76,0.40);
  }
  ```

### 2. chg% separator — holdings (right grid)
File: `frontend/src/lib/data/pulseColumns.js`
- LOTS column cellClass (line ~576) currently: `d?.qty_pos !== undefined ? [RA, 'lots-left-sep'] : [RA]`
- Holdings rows have `qty_hold` defined, not `qty_pos`. Fix: `(d?.qty_pos !== undefined || d?.qty_hold !== undefined) ? [RA, 'lots-left-sep'] : [RA]`

### 3. chg% separator — byund snapshot grid (derivatives)
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- The byund snapshot grid is a CSS Grid. Find the chg% span in the byund row template (the span showing `chgPct` or similar after the LTP span).
- Add class `byund-chg-sep` to that span.
- Add CSS in the page `<style>` block:
  ```css
  .byund-chg-sep {
    box-shadow: inset -1px 0 0 0 rgba(126,151,184,0.40);
  }
  ```

### 4. Add chg% column to legs CSS Grid
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- In the `.cand-grid` `grid-template-columns`, after the LTP column (`minmax(62px, max-content) /* ltp */`), insert: `minmax(48px, max-content) /* chg % */`
- In the cand-row-header row, add a header span `<span class="num">Chg %</span>` in the correct column position.
- In the cand-row-total row, add a `<span class="num">—</span>` placeholder (no total for chg%).
- Add CSS for right-border separator on the chg% column header cell (using nth-child or a named class).

File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`
- After the LTP span (line ~319–326), add a new span for chg%:
  ```svelte
  {@const _chgPct = c.change_pct != null ? c.change_pct :
    (typeof ltp === 'number' && typeof c.prev_close === 'number' && c.prev_close > 0
      ? (ltp - c.prev_close) / c.prev_close * 100 : null)}
  <span class="num tf-cell cand-chg-sep {ltpDayClass(_chgPct)} {flash.classOf(`${_legFlashKey}:chg`)}">
    {_chgPct != null ? pctFmt(_chgPct) : '—'}
  </span>
  ```
  Where `pctFmt` is the 2-decimal percent formatter (check existing imports for the right name).
- Remove `{flash.classOf(`${_legFlashKey}:chg`)}` from the LTP span class string (line 326) — it now lives on the chg% span.
- Add CSS for the right-border separator in CandidateLegRow.svelte:
  ```css
  .cand-chg-sep {
    box-shadow: inset -1px 0 0 0 rgba(126,151,184,0.40);
  }
  ```

### 5. Fix leg chg% flash — wire to tickBus
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- In the tickBus subscription (lines 1699–1704), after the existing `flash.update(\`leg:${k}:ltp\`, ...)` call, add:
  ```javascript
  const _pc = c.prev_close != null ? Number(c.prev_close) : 0;
  if (_pc > 0 && snap?.ltp != null) {
    flash.update(`leg:${k}:chg`, ((Number(snap.ltp) - _pc) / _pc) * 100);
  }
  ```
  This ensures chg% flash fires on every SSE tick matching a leg symbol, not just on 30s poll cycles.

### 6. Remove green tint from legs totals row
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- In the `<style>` block, after line 6025 (`.cand-row.cand-row-total > .cell-flat { ... }`), add:
  ```css
  /* Suppress cell-level green/red background in totals — amber container is the signal. */
  .cand-row.cand-row-total .cand-pnl.cell-pos,
  .cand-row.cand-row-total .cand-pnl.cell-neg { background-color: transparent !important; }
  ```

### 7. Payoff legend renames
File: `frontend/src/lib/OptionsPayoff.svelte`
- Line ~1269: `P&L` → `Day P&L`
- Line ~1284: `Exp P&L` → `Exp Val`

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(ui): legs chg% column + separator all grids, fix chg% flash, totals tint, payoff labels

## Done when
- Pinned/watchlist/winners/losers/holdings all show inset right-border on chg% column
- Holdings LOTS column shows the separator (lots-left-sep) for holding-only rows
- byund snapshot grid has separator after chg% cell
- Legs CSS Grid shows dedicated chg% column after LTP with right-border separator
- Leg chg% flash fires immediately on SSE tick (not just on poll)
- Legs totals row amber background is clean — no green tint on positive P&L cells
- Payoff overlay legend shows "Day P&L" and "Exp Val"
- svelte-check 0 errors
