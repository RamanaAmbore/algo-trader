# Plan: Reorder LTP column in positions and holdings grids

## Context
Operator wants LTP earlier in both grids so the live price is the first numeric column seen
after the symbol. Currently LTP sits after Avg in both grids.

Target orders:
- **Positions**: St · Symbol · **LTP** · Lots · Qty · Avg · P.Close · Day P&L ...
- **Holdings**: Symbol · **LTP** · Qty · Avg · P.Close · Day P&L ...

## Agents
- backend: skip
- frontend: Three column-array edits (no logic changes, ordering only):

  **Edit 1 — `frontend/src/lib/data/pulseColumns.js` (mkRightColDefs, ~line 491)**
  Move `ltpCol` from after `avg_combined` block (currently line 519) to before the `lots`
  column (currently line 493). New order: sparkCol → ltpCol → Lots → Qty → Avg → prevCol.

  **Edit 2 — `frontend/src/lib/PerformancePage.svelte` (holdingsCols, ~line 561)**
  Current order in array: Symbol, Qty (563), Avg (564), LTP (565), P.Close ...
  Move LTP (`last_price`) entry to be the second element (right after Symbol/pinned col):
  Symbol, **LTP**, Qty, Avg, P.Close ...

  **Edit 3 — `frontend/src/lib/PerformancePage.svelte` (positionsCols, ~line 660)**
  Current order: St, Symbol, Lots (694), Qty (698), Avg (699), LTP (700), P.Close ...
  Move LTP (`last_price`) entry to be right after Symbol (before Lots):
  St, Symbol, **LTP**, Lots, Qty, Avg, P.Close ...

  For every file you change or create, you MUST write or update at least one test that covers
  the changed behaviour. This is mandatory — not optional.
  - Frontend column-order change → update the relevant Vitest snapshot or Playwright spec
    that asserts column order (check `frontend/src/lib/__tests__/` for pulseColumns tests,
    and `frontend/e2e/` for derivatives/perf grid specs).

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): move LTP before Lots (positions) and before Qty (holdings) in grids

## Done when
- Positions grid (Pulse + Perf page): LTP appears before Lots column.
- Holdings grid (Pulse + Perf page): LTP appears before Qty column.
- svelte-check 0 errors.
