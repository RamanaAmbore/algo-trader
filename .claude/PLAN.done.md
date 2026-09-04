# Plan: Standardize position/holdings grid column order across all pages

## Context

Industry standard (Zerodha Kite, Groww, IBKR, thinkorswim) is Qty → Avg → LTP, placing
cost basis adjacent to current price for natural left-to-right comparison. All four grids
currently have LTP → Avg → P.Close → Qty → Lots — backwards from the standard.

**Target order:**
- Positions: `Lots | Qty | Avg | LTP | P.Close`
- Holdings:  `Qty | Avg | LTP | P.Close` (no Lots — holdings are equity, lot_size=1)

**Current order (all four grids):** `LTP → Avg → P.Close → Qty → Lots`

## Agents

- frontend: all four file changes below
- backend: skip
- backend-test: skip
- playwright: skip
- doc: skip

## Changes — frontend agent

### 1. `frontend/src/lib/PerformancePage.svelte`

**Positions grid** (ag-Grid column defs, around line 668–722):
Current sequence: `LTP(698) → Avg(699) → P.Close(700) → Qty(705) → Lots(710)`
New sequence: `Lots → Qty → Avg → LTP → P.Close`
Move the Lots colDef before Qty, keep Avg→LTP→P.Close in that order after Qty.

**Holdings grid** (ag-Grid column defs, around line 561–587):
Current sequence: `LTP(563) → Avg(564) → P.Close(565) → Qty(570) → Lots(575)`
Holdings have no meaningful Lots column (equities, lot_size=1) — remove the Lots colDef
if present. New sequence: `Qty → Avg → LTP → P.Close`

### 2. `frontend/src/lib/data/pulseColumns.js`

**Right grid** (mkRightColDefs, around line 463):
Current sequence: `LTP(493) → Avg(494) → P.Close(506) → Qty(533) → Lots(538)`
New sequence: `Lots → Qty → Avg → LTP → P.Close`
Move `lots` colDef before `qty_net`, reorder `avg_combined → ltpCol → prevCol` after Qty.

If the right grid shows both positions and holdings in one unified view, keep Lots but
let it be empty/zero for holdings rows (existing behaviour — no logic change needed).

### 3. `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`

Svelte template with span cells. Current order:
`LTP(306) → Avg(314) → P.Close(315) → Qty(323–333) → Lots(341–358)`

New cell order in template:
`Lots → Qty → Avg → LTP → P.Close`

Also update the **header row** in
`frontend/src/routes/(algo)/admin/derivatives/+page.svelte` —
the `<span>` headers for these columns must be reordered to match, AND the
`grid-template-columns` CSS track order must be updated to match the new cell sequence.
Read the current header + grid-template-columns carefully before editing to preserve
all other columns (Checkbox, St, Symbol, Account, Exp P&L, Greeks).

## Files touched

- `frontend/src/lib/PerformancePage.svelte`
- `frontend/src/lib/data/pulseColumns.js`
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

refactor(ui): standardize position grid column order to Lots→Qty→Avg→LTP→P.Close

## Done when

1. `svelte-check` — 0 errors
2. All four grids show: Lots (if applicable), Qty, Avg, LTP, P.Close in that order
3. Holdings grids show Qty, Avg, LTP, P.Close (no Lots)
4. Header row and grid-template-columns in Derivatives page match new cell order
