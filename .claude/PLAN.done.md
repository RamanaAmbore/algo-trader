# Plan: Fix payoff dropdown stale-underlying race condition

## Context
The payoff overlay dropdown sometimes shows COPPER (or any previously-viewed underlying)
as the default even when no COPPER option positions exist. Root cause: the auto-select
`$effect` at `+page.svelte:1544` has a missing case — when `selectedUnderlying` is
restored from sessionStorage cache and that underlying is no longer in the options list
after a fresh positions load, none of the existing conditions trigger and the stale value
persists indefinitely.

## Agents
- backend: skip
- frontend: Fix `+page.svelte:1575-1578` — add a "stale underlying not in options" case
  to the auto-select effect. After reading `cur` and `opts`:
  - Check `const curInOpts = opts.find(o => o.value === cur);`
  - If `!curInOpts` AND `opts[0]?.value` exists → reset `selectedUnderlying = opts[0].value`
  - Existing promote case (curIsPopular) remains unchanged below that
  Replace lines 1575-1578 in `+page.svelte` with:
  ```js
  const curInOpts = opts.find(o => o.value === cur);
  if (!curInOpts && opts[0]?.value) {
    untrack(() => { selectedUnderlying = opts[0].value; });
    return;
  }
  const curIsPopular = curInOpts?.hint === 'popular';
  if (curIsPopular && opts[0]?.hint !== 'popular') {
    untrack(() => { selectedUnderlying = opts[0].value; });
  }
  ```
  File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` lines 1575-1578.

  For every file you change or create, you MUST write or update at least one test that covers
  the changed behaviour. This is mandatory — not optional.
  - `frontend/src/` UI change → add/update a Playwright spec in `frontend/tests/` covering the changed flow
  No change ships without a corresponding test update.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: Write a Playwright spec verifying the payoff dropdown default-underlying
  behaviour in `frontend/tests/derivatives-payoff-default.spec.js` (or add to an existing
  derivatives spec). The spec should:
  (a) Navigate to the derivatives admin page
  (b) Wait for positions to load (wait for the dropdown to have a value)
  (c) Assert the dropdown shows an underlying that has positions, not an empty/placeholder value
  If the dev environment has no live positions, assert the dropdown at minimum shows a
  non-empty value (first option selected, not blank).

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): reset payoff dropdown when cached underlying no longer in options

## Done when
- When positions refresh removes a previously-selected underlying from the options list,
  `selectedUnderlying` resets to `opts[0].value` (first underlying with positions).
- svelte-check 0 errors.
