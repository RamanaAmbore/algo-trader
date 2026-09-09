# Plan: Fix flaky overlay day P&L — use positionsDayPnlStore/holdingsDayPnlStore SSOT + null guard

## Context
The DAY P&L row in the OptionsPayoff overlay disappears transiently because:
1. `candidatesDayPnl` re-derives day P&L from scratch using `livePositionDayPnl()` —
   the same function `positionsDayPnlStore` uses internally — but bypasses the store,
   so it has no connection to the values NavStrip P1 shows
2. `candidatesDayPnl` always returns a number (0 when no legs), so the
   `{#if dayPnl != null && dayPnl !== 0}` guard hides the row whenever it's transiently 0
   (during poll refresh, candidatePositions briefly empties → sum = 0 → row disappears)

Fix: replace the per-leg `livePositionDayPnl()` re-computation with a direct lookup from
`positionsDayPnlStore.byKey[sym]` (for F&O/equity positions) and
`holdingsDayPnlStore.byKey[sym]` (for equity holdings) — the same stores NavStrip P1 reads.
Return `null` when no enabled legs (row hidden cleanly), `0` when legs exist but value is zero
(row shows ₹0 during brief zero windows instead of disappearing).

## Agents

- frontend: Three changes in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

  **Step 1 — add store imports (near top of script, where other stores are imported):**
  ```javascript
  import { positionsDayPnlStore } from '$lib/data/positionsDayPnlStore.svelte.js';
  import { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js';
  ```
  Verify the exact export names by reading those two files first.

  **Step 2 — rewrite `candidatesDayPnl` (around line 1999):**

  Current block uses `livePositionDayPnl(...)` per leg. Replace with store lookups:

  ```javascript
  const candidatesDayPnl = $derived.by(() => {
      void _throttledTick;
      // Touch store reactive state so this re-derives on store updates
      void $positionsDayPnlStore;
      void $holdingsDayPnlStore;
      let s = 0;
      let hasLegs = false;
      for (const c of candidatePositions) {
          if (!_isLegEnabled(c)) continue;
          if (!_includeHoldings && c.kind === 'eq') continue;
          hasLegs = true;
          const sym = String(c.symbol || '').toUpperCase();
          const store = c.kind === 'eq' ? $holdingsDayPnlStore : $positionsDayPnlStore;
          s += store.byKey[sym] ?? 0;
      }
      return hasLegs ? s : null;
  });
  ```

  Notes:
  - `positionsDayPnlStore` and `holdingsDayPnlStore` are Svelte stores — access with `$` prefix
    OR via `.byKey` property if they export a plain object. Check the store file to confirm
    the access pattern (some stores export `{ byKey, total }` directly as a reactive object,
    others use the `$store` Svelte subscription pattern).
  - Keep `void _throttledTick` as a secondary trigger so SSE ticks also fire the recompute.
  - Remove the now-unused `livePositionDayPnl` import from the nav import line if nothing
    else in the page uses it. Search for other `livePositionDayPnl` call sites first.

  **Step 3 — fix OptionsPayoff render guard:**
  In `frontend/src/lib/OptionsPayoff.svelte`, find the DAY P&L row (around line 764):
  ```svelte
  {#if dayPnl != null && dayPnl !== 0}
  ```
  Change to:
  ```svelte
  {#if dayPnl != null}
  ```
  This shows ₹0 during brief zero windows instead of hiding the row.

- backend: skip
- backend-test: skip
- playwright: skip
- doc: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no
- vitest: yes

## Commit message
fix(derivatives): candidatesDayPnl reads positionsDayPnlStore/holdingsDayPnlStore — SSOT with NavStrip

## Done when
- `candidatesDayPnl` reads from `positionsDayPnlStore.byKey` / `holdingsDayPnlStore.byKey`
- `livePositionDayPnl` import removed from derivatives page (if no other callers remain)
- `OptionsPayoff` DAY P&L row shows ₹0 during poll gaps instead of disappearing
- svelte-check 0 errors, vitest passes
