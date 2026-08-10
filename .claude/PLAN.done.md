# Plan: Fix 2 regressions — NavStrip H slot live-pnl + LTP flash MCX key mismatch

## Task

Two bugs introduced/exposed by the prior deployment, plus one non-bug clarification.

**Item 1 — NavStrip H slot (1.55c) ≠ MarketPulse holdings TOTAL (1.72c):**
The `_accumTotalsRow` fix made TOTAL use live-LTP-recomputed `r.pnl` = `(liveHold - avgCost) × qty`
from pulseUnified. But `PositionStrip._liveHoldingsTotal` (~line 452) still reads raw broker
`h.pnl` (snapshot). During market hours, live LTP moves → the two diverge.
Fix: rewrite `_liveHoldingsTotal` to compute `(getLtp(sym) - h.average_price) × h.quantity`
per holding using symbolStore/nav.js `getLtp`, matching pulseUnified's mergeHoldingRows logic.
Fall back to `h.pnl` when live LTP is unavailable or avgCost/qty is zero.

**Item 2 — LTP flash appears only for some symbols in positions:**
tickBus emits with full contract key e.g. `"CRUDEOIL25AUGFUT"` (the symbolStore key).
`_ltpCellClass` in `pulseColumns.js` looks up using `p.data.tradingsymbol` = `"CRUDEOIL"` (bare root).
`getLtpFlashUp/Down` Set contains `"CRUDEOIL25AUGFUT"` → `"CRUDEOIL"` not in Set → no flash.
Mover rows already have `row.quote_symbol` populated in pulseUnified (line ~334). Position/holding
rows do not.
Fix: (a) in `pulseUnified.js` `mergePositionRows` and `mergeHoldingRows`, assign `row.quote_symbol`
from the broker data when the full contract symbol is available (same pattern as mover rows).
(b) in `pulseColumns.js` `_ltpCellClass` (~line 207), use
`(p.data.quote_symbol || p.data.tradingsymbol || '').toUpperCase()` as the sym for flash
lookup — matching the same pattern `mkResolveCellLtp` already uses for LTP value lookup.

**Item 3 — Dhan chip green with zero holdings value (NOT a bug):**
`_unwrap` raises RuntimeError only on `status:"failure"` responses. If Dhan returns
`status:"success"` with zero-qty holdings (F&O-only account / fully-sold positions), the chip
correctly stays green. No fix needed. The zero VALUE (not zero count) just means the
account holds equity that has no market value or has fully exited all equity positions.

## Agents

- frontend: Fix items 1 + 2.
  Files: `frontend/src/lib/PositionStrip.svelte`, `frontend/src/lib/data/pulseUnified.js`,
  `frontend/src/lib/data/pulseColumns.js`.

  **(1) PositionStrip.svelte `_liveHoldingsTotal` (~line 452):**
  - Import `getLtp` (or the equivalent live-LTP resolver) from symbolStore or nav.js — check
    what PositionStrip already imports (it likely has `livePositionDayPnl` which uses a similar
    pattern; find the `getLtp` call path from there).
  - Change the derived from `Σ h.pnl` to:
    ```javascript
    const _liveHoldingsTotal = $derived.by(() => {
      let s = 0;
      for (const h of holdings) {
        const sym = String(h.tradingsymbol || '').toUpperCase();
        const liveHold = getLtp(sym);  // from symbolStore
        const avgCost  = Number(h.average_price || 0);
        const qty      = Number(h.quantity      || 0);
        if (liveHold != null && avgCost > 0 && qty !== 0) {
          s += (liveHold - avgCost) * qty;
        } else {
          s += Number(h.pnl || 0);  // fallback to broker snapshot
        }
      }
      return s;
    });
    ```
  - Update the stale comment above it (mentions that MarketPulse uses `_broker_pnl`, which
    is no longer true).

  **(2a) pulseUnified.js `mergePositionRows` and `mergeHoldingRows`:**
  - After setting `row.tradingsymbol`, also set `row.quote_symbol` from the broker data when
    available — check what field the broker provides for the full contract symbol (e.g.,
    `r.quote_symbol`, `r.instrument_token` mapped back, or the symbolStore lookup key).
    If the broker data doesn't include a separate quote_symbol field, check if there's an
    `instrument_token` → symbol reverse-lookup available, or simply copy `tradingsymbol` as
    the `quote_symbol` initially (the important case is when they differ for MCX).
    Look at how mover rows populate `row.quote_symbol` at ~line 334 and apply the same pattern.

  **(2b) pulseColumns.js `_ltpCellClass` (~line 207):**
  - Change `const sym = String(p.data.tradingsymbol || '').toUpperCase();` to:
    `const sym = String(p.data.quote_symbol || p.data.tradingsymbol || '').toUpperCase();`
  - This mirrors `mkResolveCellLtp`'s existing pattern and ensures MCX full-contract symbols
    match what's in the flash Set.

  For every file changed, write or update at least one Playwright spec in `frontend/tests/`
  covering the changed flow. No change ships without a test.

- broker: skip
- backend: skip
- backend-test: skip
- playwright: skip
- doc: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(frontend): NavStrip H slot live-pnl sync + LTP flash MCX symbol key

## Done when
- NavStrip H slot lifetime-pnl matches MarketPulse holdings TOTAL during live market hours
- LTP flash appears for MCX positions (CRUDEOIL, NATURALGAS, etc.) consistently across all grids
- svelte-check 0 errors
