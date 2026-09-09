# Plan: Fix payoff overlay auto-selection — sort by qty + provisional flag + localStorage

## Context

The derivatives payoff overlay fails to auto-select CRUDEOIL/GOLDM reliably, instead
showing COPPER or NIFTY. Three independent failure modes converge:

**Failure 1 — Alphabetical sort bug (COPPER beats CRUDEOIL)**  
Tier 2 (futures-only) sorts by position-count desc then alphabetical. When CRUDEOIL,
GOLDM, and COPPER each have 1 position row, alphabetical tiebreaker puts COPPER first.

**Failure 2 — Provisional-seed watchlist trap (NIFTY sticks)**  
Cold-start sequence: (a) cold-start provisional seed fires (`_provisionalSeed → true`,
`selectedUnderlying = 'NIFTY'`), (b) watchlist loads → NIFTY moves from Tier 6
(`hint='popular'`) to Tier 4 (`hint='pinned'`), (c) positions load → promote condition
checks `curIsPopular` (false — now 'pinned') → doesn't fire → NIFTY sticks even though
CRUDEOIL is in Tier 2.

**Failure 3 — Race condition (inconsistent result)**  
On warm cache, positions may arrive before watchlist. On cold cache, order is random
depending on API latency. Each ordering fires the auto-select effect with different
opts[0], selecting different underlyings. localStorage would pin the selection after the
first correct visit, eliminating all subsequent races.

## Agents

- frontend: Three targeted changes to
  `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

  **Change 1 — Sort by |qty| (lines ~1468–1490)**

  In `underlyingOptionsForPicker`, alongside the existing `_rootPosCount` map, build a
  `_rootQtySum` map summing `Math.abs(Number(p.qty ?? 0))` per root. Change the Tier 1
  and Tier 2 sort to: `_rootQtySum desc → _rootPosCount desc → alphabetical`.

  ```javascript
  // existing:
  const _rootPosCount = new Map();
  // add:
  const _rootQtySum = new Map();

  // in the positions loop (line ~1470):
  _rootPosCount.set(r, (_rootPosCount.get(r) || 0) + 1);
  _rootQtySum.set(r, (_rootQtySum.get(r) || 0) + Math.abs(Number(p.qty ?? 0)));

  // Tier 1 sort (line ~1477):
  [..._rootsWithOptions].sort((a, b) =>
    (_rootQtySum.get(b) || 0) - (_rootQtySum.get(a) || 0) ||
    (_rootPosCount.get(b) || 0) - (_rootPosCount.get(a) || 0) ||
    a.localeCompare(b))

  // Tier 2 sort (line ~1485): same pattern
  ```

  Effect: CRUDEOIL 2 lots beats COPPER 1 lot. Multi-account positions accumulate.

  **Change 2 — `_provisionalSeed` flag (lines ~3954–3957 + effect ~1582–1585)**

  Declare at the top of the script section:
  ```javascript
  let _provisionalSeed = $state(false);
  ```

  In the cold-start NIFTY seed block (line ~3956), set the flag:
  ```javascript
  selectedUnderlying = POPULAR_UNDERLYINGS[0];
  _provisionalSeed = true;
  ```

  In the auto-selection `$effect` (line ~1582), extend the promote condition from
  `curIsPopular` to `curIsPopular || _provisionalSeed`, and reset on promote:
  ```javascript
  const curIsPromotable = curInOpts?.hint === 'popular' || _provisionalSeed;
  if (curIsPromotable && (opts[0]?.hint === 'options' || opts[0]?.hint === 'futures')) {
    _provisionalSeed = false;
    untrack(() => { selectedUnderlying = opts[0].value; });
  }
  ```

  Also add `void _provisionalSeed;` to the tracked sources list at the top of the
  $effect (alongside the existing `void positions; void holdings;` etc.).

  Effect: even after watchlist absorbs NIFTY into Tier 4 (changing hint to 'pinned'),
  `_provisionalSeed` stays true → promote fires when positions arrive. One-shot:
  `_provisionalSeed = false` after first promote prevents fighting later manual picks.

  **Change 3 — localStorage persistence (lines ~326–334 + new $effect)**

  In onMount #1 (after URL param read, before it ends), if no URL param was applied,
  attempt to restore from localStorage:
  ```javascript
  if (!selectedUnderlying) {
    try {
      const saved = localStorage.getItem('ramboq.derivatives.underlying');
      if (saved) selectedUnderlying = saved.toUpperCase().trim();
    } catch {}
  }
  ```

  Add a dedicate $effect to persist selection changes (near the URL sync $effect):
  ```javascript
  $effect(() => {
    if (selectedUnderlying) {
      try { localStorage.setItem('ramboq.derivatives.underlying', selectedUnderlying); } catch {}
    }
  });
  ```

  The auto-selection effect already handles stale saved selection (Case 2: if saved
  symbol is not in opts → resets to opts[0]). No additional guard needed.

  Effect: on return visits the selection is immediately stable — no race condition, no
  dependency on positions/watchlist load order.

- backend-test: Update `frontend/e2e/derivatives_pulse_day_pnl_ssot.spec.js` — add a
  new test block (or new file `frontend/e2e/derivatives_auto_select.spec.js`) verifying:
  1. `_provisionalSeed` state variable is declared in the derivatives page source
  2. `_rootQtySum` map is built and used in the Tier 1/2 sort
  3. `localStorage.setItem('ramboq.derivatives.underlying'` is present
  4. `localStorage.getItem('ramboq.derivatives.underlying'` is present in onMount

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

fix(derivatives): auto-select by qty-sort + provisional-seed flag + localStorage persistence

## Done when

- Tier 1/2 sort uses `|qty| desc` as primary key — CRUDEOIL with 2 lots ranks before COPPER with 1
- `_provisionalSeed` flag causes promote to fire even after watchlist absorbs NIFTY into Tier 4
- `selectedUnderlying` persisted to localStorage — return visits restore last selection instantly
- svelte-check 0 errors, playwright spec green
