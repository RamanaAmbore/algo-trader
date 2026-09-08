# Plan: Fix day P&L SSOT — overlay candidatesDayPnl must use livePositionDayPnl

## Context
The derivatives overlay `candidatesDayPnl` and NavStrip P1 (`positionsDayPnlStore.total`)
use different formulas for per-leg day P&L, causing the sum of per-root overlay day P&Ls
(CRUDEOIL + GOLDM) to not equal NavStrip P1.

Current overlay per-leg formula (two-step):
  1. `_dayPnlForLeg(c)` → `(legLiveLtp − close) × qty`  OR  `baseDayPnlForPosition(c)` fallback
  2. `+ delta = (liveLtp − pollLtp) × qty` only when step 1 fell back

NavStrip formula (`livePositionDayPnl`):
  `realisedToday = brokerDcv − (pollLtp − close) × qty`
  `return realisedToday + (live − close) × qty`

These converge algebraically when `brokerDcv = (pollLtp − close) × qty` (simple case) but
diverge for Case 2 overnight positions (dcv=0, pnl≠0) and when live-LTP reads happen at
different reactive granularities.

Fix: in `candidatesDayPnl`, replace the `_dayPnlForLeg + delta` two-step with a direct call
to `livePositionDayPnl()` (the SSOT function already used by `positionsDayPnlStore`).
`_dayPnlForLeg` is kept for other callers (e.g. `_legExpPnlDisplay`); only `candidatesDayPnl`
switches to the unified formula.

## Agents

- frontend: In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

  **Step 1 — add `livePositionDayPnl` to the nav import (line ~60):**
  Change:
  ```javascript
  import { baseDayPnlForPosition, FO_EXCHANGES } from '$lib/data/nav';
  ```
  To:
  ```javascript
  import { baseDayPnlForPosition, livePositionDayPnl, FO_EXCHANGES } from '$lib/data/nav';
  ```

  **Step 2 — rewrite the per-leg computation inside `candidatesDayPnl` (lines ~1999-2025):**

  The current block iterates `candidatePositions`, calls `_dayPnlForLeg(c, liveSpot)`, then
  conditionally adds a `(liveLtp − pollLtp) × qty` delta. Replace the per-leg P&L section with:

  ```javascript
  const candidatesDayPnl = $derived.by(() => {
      void _throttledTick;
      let s = 0;
      for (const c of candidatePositions) {
          if (!_isLegEnabled(c)) continue;
          if (!_includeHoldings && c.kind === 'eq') continue;
          const legLiveLtp = untrack(() => getSnapshot(String(c.symbol || '').toUpperCase())?.ltp);
          const day = livePositionDayPnl(
              {
                  closePx:  Number(c.prev_close ?? 0),
                  pollLtp:  Number(c.ltp || 0),
                  qty:      Number(c.qty || 0),
                  avg:      Number(c.average_price || c.avg_cost || 0),
                  dcvRow:   c,
              },
              legLiveLtp ?? null,
              { marketOpen: _isMarketOpen },
          );
          s += day;
      }
      return s;
  });
  ```

  Notes:
  - `_isMarketOpen` — verify the exact variable name used in the page for market-open state
    (search for `isMarketOpen` or `_isMarketOpen` or `marketOpen` in the reactive block area);
    use whatever name is already in scope.
  - `c.average_price || c.avg_cost` — check which field positions use for entry price;
    use whichever is populated.
  - `_dayPnlForLeg` function itself is NOT removed — it's still used by `_legExpPnlDisplay`
    and other callers. Only `candidatesDayPnl` switches to `livePositionDayPnl`.
  - Remove the now-unused `oq`, `close`, `qty`, `day`, `pollLtp`, `liveLtp`, `dayPnlUsedLive`,
    `delta` variables from the old block.

- backend-test: Add a Vitest test in `frontend/src/lib/__tests__/data/` verifying that
  `livePositionDayPnl` produces the correct result for the key cases that previously diverged:

  **Test A — Case 2 overnight (dcv=0, pnl≠0):**
  ```javascript
  it('Case 2: dcv=0 overnight position uses pnl-based rescue', () => {
      const result = livePositionDayPnl(
          { closePx: 5800, pollLtp: 5850, qty: 1, avg: 5700,
            dcvRow: { qty: 1, overnight_quantity: 1, day_change_val: 0, pnl: 150,
                      previous_close: 5800, average_price: 5700 } },
          5860,  // liveLtp
          { marketOpen: true }
      );
      // realisedToday = baseDayPnlForPosition(dcvRow) - (5850-5800)*1 = 150-(50) = 100
      // return 100 + (5860-5800)*1 = 100 + 60 = 160
      expect(result).toBeCloseTo(160, 1);
  });
  ```

  **Test B — simple overnight (dcv matches (ltp-close)×qty):**
  ```javascript
  it('simple overnight: result equals (livePrice - close) * qty', () => {
      const result = livePositionDayPnl(
          { closePx: 5800, pollLtp: 5850, qty: 1, avg: 5700,
            dcvRow: { qty: 1, overnight_quantity: 1, day_change_val: 50, pnl: 150,
                      previous_close: 5800, average_price: 5700 } },
          5860,
          { marketOpen: true }
      );
      // realisedToday = 50 - (5850-5800)*1 = 0 ; return 0 + (5860-5800)*1 = 60
      expect(result).toBeCloseTo(60, 1);
  });
  ```

  Import `livePositionDayPnl` from `$lib/data/nav.js`. Place in a new file
  `frontend/src/lib/__tests__/data/livePositionDayPnl.test.js` or append to an existing
  nav-related test file if one exists.

- playwright: skip
- doc: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no
- vitest: yes

## Commit message
fix(derivatives): candidatesDayPnl uses livePositionDayPnl — SSOT with positionsDayPnlStore

## Done when
- `candidatesDayPnl` delegates to `livePositionDayPnl()` for every leg
- `_dayPnlForLeg` untouched (still used by `_legExpPnlDisplay`)
- svelte-check 0 errors, vitest passes
- CRUDEOIL overlay day P&L + GOLDM overlay day P&L ≈ NavStrip P1 positions total
