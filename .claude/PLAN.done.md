# Plan: Group LTP → Avg → P.Close in sequence and rename Close → P.Close across all grids

## Context
The Close (previous session close) column is out of sequence in every position/holdings grid — it sits after Day P&L / Day % instead of immediately after Avg. Two changes needed: (1) move Close to be immediately after Avg so LTP → Avg → P.Close are grouped, and (2) rename the header from "Close" to "P.Close" everywhere.

## Affected pages
- `/pulse` (public) — MarketPulse right grid
- `/dashboard` (algo) — MarketPulse right grid (same component as Pulse)
- `/performance` (public) — holdings grid + positions grid
- `/admin/derivatives` (algo) — Legs/Exp-close grid (header + data rows) + Snapshot card

## Files to change

### 1. `frontend/src/lib/data/pulseColumns.js`

**`mkPrevCol` factory (~line 261–274):**
Change `headerName: 'Close'` → `headerName: 'P.Close'`.
This renames Close in both the MarketPulse left grid (movers/watchlist) and wherever prevCol is used in mkRightColDefs.

**`mkRightColDefs` (~line 503–561):**
Move `prevCol` from its current position after the `day_pnl_pct` block (line 525) to immediately after the `avg_combined` block (line 514).

Current order: `ltpCol | avg_combined | day_pnl | day_pnl_pct | prevCol | pnl | ...`
Target order:  `ltpCol | avg_combined | prevCol | day_pnl | day_pnl_pct | pnl | ...`

### 2. `frontend/src/lib/PerformancePage.svelte`

**`holdingsCols` (~line 562–587):**
- Change `headerName: 'Close'` → `headerName: 'P.Close'` on the `close_price` row (line 569)
- Move that row to right after `average_price` (line 564)

Current order: `last_price | average_price | day_change_val | day_change_percentage | pnl | pnl_percentage | close_price | ...`
Target order:  `last_price | average_price | close_price | day_change_val | day_change_percentage | pnl | pnl_percentage | ...`

**`positionsCols` (~line 698–720):**
- Change `headerName: 'Close'` → `headerName: 'P.Close'` on the `close_price` row (line 704)
- Move that row to right after `average_price` (line 699)

Same reorder as holdingsCols.

### 3. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

**Legs/Exp-close grid header (~line 4534–4544):**
- Rename `<span class="num">Close</span>` → `<span class="num">P.Close</span>` (line 4540)
- Move that `<span>` from after Day P&L (line 4540) to right after the Avg `<span>` (line 4535)

Current header: `... | LTP | Avg | Day P&L | Close | P&L | ...`
Target header:  `... | LTP | Avg | P.Close | Day P&L | P&L | ...`

**Snapshot card header (~line 4780):**
- Rename `<span class="num" title="...">Close</span>` → `P.Close` (keep title attribute)
- No reorder needed (no Avg column in Snapshot)

### 4. `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`

**Data cells (~line 331–347):**
Move the prev_close cell (line 344: `{c.prev_close != null ? priceFmt(c.prev_close) : '—'}`)
from after the Day P&L cell (line 340–343) to right after the Avg/cost cell (line 339).

Current cell order: `LTP (331) | Avg (339) | Day P&L (340–343) | Close (344) | P&L (345) | ...`
Target cell order:  `LTP (331) | Avg (339) | Close (344) | Day P&L (340–343) | P&L (345) | ...`

## Agents
- backend: skip
- frontend: Make the column reorder (LTP → Avg → P.Close) and header rename (Close → P.Close) in all four files listed above. Pure ordering + string changes — no logic changes. For every file you change, you MUST write or update at least one Vitest test covering the column order and header name.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): group LTP → Avg → P.Close in sequence and rename Close to P.Close across all position, holdings, and derivatives grids

## Done when
- `mkPrevCol` in pulseColumns.js has `headerName: 'P.Close'`
- `mkRightColDefs` has prevCol immediately after avg_combined
- `PerformancePage` holdingsCols and positionsCols have `headerName: 'P.Close'` and close_price immediately after average_price
- Derivatives Legs header has `P.Close` immediately after `Avg`
- `CandidateLegRow` prev_close cell is immediately after the Avg/cost cell
- Derivatives Snapshot header has `P.Close`
- `npx svelte-check` passes with 0 errors
