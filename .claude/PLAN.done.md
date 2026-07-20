# Plan: GTT-QTY-GUARD stale-index fallback + RefreshButton default-mode cleanup

## Task

**P0 — CRUDEOIL GTT attach refused overnight**: At 00:19 IST (post MCX close), the
instruments cache TTL expired and `get_lot_size("MCX", "CRUDEOIL25AUGFUT")` failed.
The GTT-QTY-GUARD (A4 from 6D sprint) returned 0 (cache miss sentinel), which the
`_resolve_lot_size_for_order` guard treats as "cannot safely translate qty → refuse".
Result: exits NOT attached to the position. Fix: when live fetch fails, fall back to
the stale `_LOT_INDEX` module-level dict (populated from last successful fetch, persists
all process-lifetime). Covers overnight MCX window where cache expires but prior index
is still warm.

**Frontend — RefreshButton default-mode cleanup**: Two violations of the canonical
CardControls gate (`onRefresh && (isFullscreen || refreshAlwaysVisible)`):
1. `derivatives/+page.svelte:4221` — `refreshAlwaysVisible={true}` on the Legs card
   shows RefreshButton always (should only show in fullscreen).
2. `PerformancePage.svelte:1282` — standalone `<RefreshButton>` inside `{#if !compactHeader}`
   block. PerformancePage is always embedded inside a page that has a page-header RefreshButton.
   The per-component internal one is redundant in default mode and violates the rule.
Rule per operator: page-header RefreshButton is canonical for all default-mode pages.
Card-level RefreshButton only in fullscreen.

## Agents

- backend: skip
- frontend: In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` line 4221,
  remove `refreshAlwaysVisible={true}` (delete the line). In
  `frontend/src/lib/PerformancePage.svelte` line 1282, remove the
  `<RefreshButton onClick={() => loadAll({ fresh: true })} {loading} label="performance" />`
  line. Check if `RefreshButton` import in `PerformancePage.svelte` is still used elsewhere
  in that file; if not, remove the import too. Run svelte-check after.
- broker: In `backend/brokers/adapters/kite.py`, inside `get_lot_size`, in the
  `except Exception as e` handler (currently lines 178-182), add a stale-index fallback
  BEFORE returning 0: check `_LOT_INDEX.get((exchange, tradingsymbol))`; if it returns
  a value > 1, log a warning and return it. Only if stale index is also empty, return
  0 for MCX (or 1 for non-MCX) as today. Change the `logger.debug` on line 179 to
  `logger.warning`. Also write a new test in `backend/tests/test_audit_remediation.py`:
  "test_get_lot_size_stale_index_fallback" — mock `get_or_fetch` to raise RuntimeError,
  pre-populate `_LOT_INDEX` with `("MCX", "CRUDEOIL25AUGFUT") → 100`, assert
  `get_lot_size("MCX", "CRUDEOIL25AUGFUT")` returns 100 (not 0).
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(gtt-guard): stale-index fallback in get_lot_size + RefreshButton default-mode cleanup

GTT-QTY-GUARD (A4) refused CRUDEOIL25AUGFUT attach at 00:19 IST: instruments cache
expired overnight, get_lot_size returned 0 sentinel, guard blocked attach. Fix:
use stale _LOT_INDEX (warm from last successful fetch) before returning 0. Handles
overnight MCX cache-expiry window without weakening the guard on cold start.

Frontend: remove refreshAlwaysVisible={true} from Legs card and standalone <RefreshButton>
from PerformancePage — page-header refresh button is canonical for default mode.

## Done when

- `backend/tests/test_audit_remediation.py::test_get_lot_size_stale_index_fallback` passes
- `kite.py get_lot_size` except block: stale index checked before returning 0
- `derivatives/+page.svelte`: `refreshAlwaysVisible={true}` line removed (Legs card)
- `PerformancePage.svelte`: standalone `<RefreshButton>` removed from internal header
- All pytest passes; svelte-check 0 errors
- RefreshButton import removed from PerformancePage if no longer used
