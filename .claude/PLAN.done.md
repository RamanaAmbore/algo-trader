# Plan: Fix P-slot/Pulse day P&L sync regression — invert SSOT direction

## Task
The previous fix (dual-store merge in positionsDayPnlStore) introduced a regression:

**Root cause of regression**: `positionsDayPnlStore` now merges both `positionsStore`
(may have stale localStorage cache from a previous session, e.g. dcv=+7k) and
`pulsePositionsStore` (fresh). The merge iteration order `[...p1, ...p2]` (positionsStore
first) means the stale +7k wins. The SSOT override in `buildUnified` then fires for ALL
position symbols (byKey is now populated), replacing Pulse's accurate cq-computed -6k
with the stale +7k. BHEL shows 0 because positionsDayPnlStore now has a 0 entry for it
(NSE equity, no SSE tick → falls to stale broker dcv=0), and the override fires with 0,
replacing the cq-computed correct value.

**Correct architectural fix**: Invert the SSOT direction.
- BEFORE (broken): positionsDayPnlStore computes from broker dcv → overrides Pulse per-row
- AFTER (correct): Pulse computes from cq (live quotes) → writes result to positionsDayPnlStore → NavStrip reads from store

This means Pulse rows are always cq-accurate, and NavStrip total mirrors Pulse because
Pulse writes its computed total/byKey to the store after each buildUnified.

## Agents
- backend: skip
- frontend: Undo the merge regression and invert the SSOT direction.

  **Step 1 — Revert positionsDayPnlStore.svelte.js**:
  Read the current file first. Remove the `pulsePositionsStore` import and the
  `mergePositionStores` import and call. Restore `const rows = positionsStore.value ?? [];`
  (back to positionsStore only). Restore the original import line to only import
  `positionsStore`.

  **Step 2 — Add setFromPulse to positionsDayPnlStore**:
  Add two module-level `$state` variables:
  ```js
  let _pulseTotal = $state(/** @type {number|null} */ (null));
  let _pulseByKey = $state(/** @type {Record<string,number>|null} */ (null));
  ```
  Update the exported object to:
  ```js
  export const positionsDayPnlStore = {
    get total() { return _pulseTotal ?? _store.total; },
    get byKey()  { return _pulseByKey ?? _store.byKey;  },
    /**
     * Called by MarketPulse after each buildUnified with cq-accurate per-symbol
     * and aggregate values. Takes priority over the SSE-only _store computation.
     * @param {Record<string,number>} byKey
     * @param {number} total
     */
    setFromPulse(byKey, total) {
      _pulseByKey = byKey;
      _pulseTotal = total;
    },
  };
  ```

  **Step 3 — Remove the SSOT position override from MarketPulse.svelte**:
  Read `frontend/src/lib/MarketPulse.svelte` around lines 2949-2956. Find and remove the
  positions SSOT override block:
  ```js
  // SSOT override: positionsDayPnlStore wins for day_pnl on every position row.
  for (const [exSym, val] of Object.entries(positionsDayPnlStore.byKey)) {
    const sym = exSym.split(':').pop();
    const row = byKey[`${sym}__pos`];
    if (row) row.day_pnl = val;
  }
  ```
  This block must be deleted entirely. Pulse will now use the cq-computed day_pnl from
  mergePositionRows directly (which is the accurate value using live quote LTP).

  **Step 4 — Add Pulse → store write in MarketPulse.svelte**:
  After removing the SSOT override, add a `$effect` in MarketPulse that writes the
  positions day_pnl aggregate to positionsDayPnlStore after each unifiedRows update.
  Place it near the other `$effect` blocks (e.g., near the prefetch effect at line ~2914):
  ```js
  // Pulse is the authoritative source for positions day P&L — it uses live cq
  // quotes. Write the computed aggregate to positionsDayPnlStore so NavStrip P
  // reads the same value without recomputing.
  $effect(() => {
    const posRows = unifiedRows.filter(r => r._majorGroup === 'positions');
    /** @type {Record<string, number>} */
    const pulseByKey = {};
    let pulseTotal = 0;
    for (const r of posRows) {
      const sym = String(r?.tradingsymbol || r?.symbol || '').toUpperCase();
      if (!sym) continue;
      const v = r.day_pnl ?? 0;
      pulseByKey[sym] = (pulseByKey[sym] ?? 0) + v;
      pulseTotal += v;
    }
    positionsDayPnlStore.setFromPulse(pulseByKey, pulseTotal);
  });
  ```
  Make sure `positionsDayPnlStore` is imported in MarketPulse.svelte (it likely already is
  for the SSOT override block).

  **Step 5 — Delete mergePositionStores.js**:
  Delete `frontend/src/lib/data/mergePositionStores.js` — no longer used.

  **Step 6 — Update/replace the Vitest test file**:
  File: `frontend/src/lib/__tests__/data/positionsDayPnlDualStore.test.js`
  The existing tests import from `mergePositionStores.js` (now deleted). Replace them with
  tests for the `setFromPulse` mechanism. Since positionsDayPnlStore is a module-level
  singleton (hard to unit-test directly), test the observable behavior:
  - Import positionsDayPnlStore
  - Call `positionsDayPnlStore.setFromPulse({NIFTY25AUGFUT: -6000}, -6000)`
  - Assert `positionsDayPnlStore.total === -6000`
  - Assert `positionsDayPnlStore.byKey['NIFTY25AUGFUT'] === -6000`
  - Call `positionsDayPnlStore.setFromPulse({}, 0)`; assert total falls back to _store (which is 0 in test env)

  For every file you change or create, you MUST write or update at least one test that covers
  the changed behaviour. This is mandatory.

  Run `cd frontend && npx svelte-check --output machine 2>&1 | tail -5` and
  `cd frontend && npx vitest run 2>&1 | tail -8` to verify before reporting done.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(pnl): invert P-slot SSOT direction — Pulse writes cq-accurate day P&L to positionsDayPnlStore instead of store overriding Pulse rows

## Done when
- Pulse positions day P&L shows cq-accurate values (no SSOT override degrading them)
- NavStrip P slot reads the same total that Pulse computed (via setFromPulse)
- BHEL and NSE equity positions show correct non-zero day P&L in Pulse (cq-computed)
- F&O option rows show correct day P&L in Pulse (same regression root cause)
- mergePositionStores.js deleted
- svelte-check 0 errors, vitest passes
