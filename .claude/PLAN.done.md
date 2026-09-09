# Plan: Fix payoff overlay auto-select — qty-sort only (revert + targeted sort fix)

## Context

Commit `b5c1ea68` introduced three changes: qty-sort, _provisionalSeed flag, and localStorage
persistence. The localStorage restore (`selectedUnderlying` set before positions load in onMount)
causes `loadStrategy({ clear: true })` to fire with empty `legs` → `strategy = null` → payoff
chart shows "No legs selected / pick legs to see payoff" until the 5s poll re-triggers the fetch.
The `_provisionalSeed` flag also has a P1 audit bug (not cleared on manual pick → can override
operator's explicit selection). The PULSE_SPEC doc (`bdb3209c`) references the reverted approach.

**Root cause of COPPER > CRUDEOIL auto-select**: Tier 1/2 sorts by position-count desc then
alphabetical asc. COPPER ('O') < CRUDEOIL ('R') alphabetically → COPPER wins when both have
1 position row. Fix: add `_rootQtySum` (sum of `|qty|` per root) as primary sort key.

## Agents

- frontend: Two steps:

  **Step 1 — Revert commits**

  Run:
  ```bash
  git revert --no-commit b5c1ea68
  git revert --no-commit bdb3209c
  git checkout HEAD -- frontend/e2e/derivatives_auto_select.spec.js 2>/dev/null || true
  ```
  After revert, verify `_provisionalSeed`, `localStorage.getItem('ramboq.derivatives.underlying')`,
  and `localStorage.setItem(...)` are NOT present in `+page.svelte`.

  **Step 2 — Apply qty-sort only (Change 1 from the reverted commit)**

  In `underlyingOptionsForPicker` in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:

  Find where `const _rootPosCount = new Map()` is declared. Add alongside it:
  ```javascript
  const _rootQtySum = new Map();
  ```

  In the positions loop that accumulates `_rootPosCount`, also accumulate:
  ```javascript
  _rootQtySum.set(r, (_rootQtySum.get(r) || 0) + Math.abs(Number(p.qty ?? 0)));
  ```

  Change the Tier 1 sort (on `_rootsWithOptions`) from:
  ```javascript
  .sort((a, b) => (_rootPosCount.get(b) || 0) - (_rootPosCount.get(a) || 0) || a.localeCompare(b))
  ```
  to:
  ```javascript
  .sort((a, b) =>
    (_rootQtySum.get(b) || 0) - (_rootQtySum.get(a) || 0) ||
    (_rootPosCount.get(b) || 0) - (_rootPosCount.get(a) || 0) ||
    a.localeCompare(b))
  ```

  Change the Tier 2 sort (on `_rootsWithFuturesOnly`) the same way.

  **Step 3 — Write/update playwright spec**

  Update or recreate `frontend/e2e/derivatives_auto_select.spec.js` to be a static
  source-inspection spec with ONLY the qty-sort assertions (no localStorage or
  provisional-seed assertions):
  1. `_rootQtySum` Map is declared in `underlyingOptionsForPicker`
  2. `_rootQtySum` is accumulated with `Math.abs(Number(p.qty ?? 0))`
  3. `_rootQtySum.get(b) - _rootQtySum.get(a)` is the primary sort key in Tier 1
  4. Same pattern in Tier 2
  5. `_provisionalSeed` is NOT present in the source (regression guard)
  6. `localStorage.getItem('ramboq.derivatives.underlying')` is NOT present (regression guard)

- backend-test: skip
- doc: Update `docs/specs/PULSE_SPEC.md` — revert the §17.2 auto-select changes added in
  `bdb3209c` back to how they were before (remove provisional-seed, localStorage, and the
  3-tier sort documentation; restore the prior description or remove if it didn't exist).

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(derivatives): auto-select sort by |qty| desc — CRUDEOIL beats COPPER on equal count

## Done when
- `b5c1ea68` and `bdb3209c` effects are reverted
- Tier 1/2 sort uses `|qty| desc → count desc → alpha` only
- No `_provisionalSeed` or `localStorage` changes in derivatives page
- svelte-check 0 errors, playwright spec green
- Payoff chart loads normally (no regression from localStorage early-restore)
