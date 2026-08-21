# Plan: Fix broker modal nav block + P-slot/Pulse day P&L sync

## Task
Two production bugs:

1. **Nav clicks blocked when broker-health modal open** — `.bh-overlay` in
   `BrokerHealthBadge.svelte` has `z-index: 9990` (nav is `--z-nav: 50`) and no
   `pointer-events: none`. Full-viewport overlay eats all nav clicks. Fix: add
   `pointer-events: none` to `.bh-overlay` CSS rule. ESC key and X button already provide
   dismiss — backdrop-click dismiss is sacrificed and removed.

2. **NavStrip P slot shows 0 while Pulse positions shows −6k** — `positionsDayPnlStore` reads
   only `positionsStore.value` (cross-page poller). Pulse uses isolated `pulsePositionsStore`
   (different dedup key). On first mount, Pulse loads via `loadPulse()` →
   `pulsePositionsStore` has rows; `positionsStore` may still be empty.
   `positionsDayPnlStore.byKey` has no entries → `buildUnified`'s SSOT override loop skips
   those symbols → Pulse shows `mergePositionRows` cq-computed value (−6k) while NavStrip
   reads `positionsDayPnlStore.total` = 0. Fix: merge both stores in
   `positionsDayPnlStore`, preferring rows with non-zero `day_change_val`.

## Agents
- backend: skip
- frontend: Two independent fixes in one agent:

  **Fix A — BrokerHealthBadge.svelte** (`frontend/src/lib/BrokerHealthBadge.svelte`):
  Read the file first. Find the `.bh-overlay` CSS rule (around line 144). Add:
  ```css
  pointer-events: none;
  ```
  This makes the overlay click-transparent — nav links become clickable again.
  Also remove the `onclick` from the overlay `<div>` since it won't receive clicks with
  `pointer-events: none`. Change:
    `<div class="bh-overlay" role="presentation" onclick={() => open = false}>`
  to:
    `<div class="bh-overlay" role="presentation">`

  **Fix B — positionsDayPnlStore.svelte.js**
  (`frontend/src/lib/data/positionsDayPnlStore.svelte.js`):
  1. Replace the existing `positionsStore`-only import with:
     ```js
     import { positionsStore, pulsePositionsStore } from '$lib/data/marketDataStores.svelte.js';
     ```
  2. In `_store`'s `$derived.by`, replace `const rows = positionsStore.value ?? [];` with:
     ```js
     const p1 = positionsStore.value ?? [];
     const p2 = pulsePositionsStore.value ?? [];
     // Merge both stores; prefer row with non-zero day_change_val over stale zero.
     // positionsStore drives the cross-page poller; pulsePositionsStore drives Pulse's
     // loadPulse() — isolated dedup keys mean one may populate before the other.
     const _bySymAcct = new Map();
     for (const r of [...p1, ...p2]) {
       const k = `${(r.tradingsymbol || r.symbol || '')}:${r.account || ''}`;
       const existing = _bySymAcct.get(k);
       if (!existing) { _bySymAcct.set(k, r); }
       else if (Number(r.day_change_val) !== 0 && Number(existing.day_change_val) === 0) {
         _bySymAcct.set(k, r);
       }
     }
     const rows = [..._bySymAcct.values()];
     ```

  For every file you change or create, you MUST write or update at least one test:
  - Fix A: add a Playwright spec (or update an existing broker-health spec if one exists)
    verifying that when the broker chip modal is open, a nav link remains clickable.
  - Fix B: add a Vitest test in `frontend/src/lib/__tests__/data/` covering:
    (a) `positionsStore` empty + `pulsePositionsStore` has rows → total = sum of pulse rows
    (b) same symbol in both stores with dcv=0 in one and dcv=-6000 in the other →
        merged row uses dcv=-6000

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip (frontend agent handles Playwright)

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(nav,pnl): broker modal pointer-events + positionsDayPnlStore dual-store merge for P-slot sync

## Done when
- Clicking nav links while broker health modal is open navigates correctly
- NavStrip P slot and Pulse positions day P&L agree from first page mount
- svelte-check clean + Vitest passes
