# Plan: derivatives picker falls back to pulsePositionsStore when positionsStore empty

## Task
The derivatives page fetches positions independently via `positionsStore` (key: `md.positions`).
When Kite REST returns 502 (e.g. nightly maintenance), `positionsStore` is empty and the
provisional NIFTY seed is never promoted — so CRUDEOIL (already loaded in `pulsePositionsStore`
by the Pulse page) is never auto-selected.

Fix: in `loadPositions()`, after the `positionsStore.load()` call, fall back to
`pulsePositionsStore.value` when `positionsStore.value` is empty/null. Also update the
provisional seed guard so it doesn't plant a NIFTY seed when pulse positions are available.

## Agents
- frontend: In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:
  1. Add `pulsePositionsStore` to the existing import from `$lib/data/marketDataStores.svelte.js` (line ~26).
  2. In `loadPositions()` (line ~3497), replace `for (const p of (positionsStore.value ?? []))` with a fallback:
     ```js
     const _posSource = positionsStore.value?.length
       ? positionsStore.value
       : (pulsePositionsStore.value ?? []);
     for (const p of _posSource) {
     ```
  3. Update the provisional seed guard (line ~3827) from:
     `if (!selectedUnderlying && !(positionsStore.value?.length))`
     to:
     `if (!selectedUnderlying && !(positionsStore.value?.length) && !(pulsePositionsStore.value?.length))`
  No other logic changes. No changes to `marketDataStores.svelte.js` — the store isolation is intentional.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): fall back to pulsePositionsStore for symbol picker when positionsStore empty

## Done when
`npx svelte-check` exits 0. CRUDEOIL (or any position from the Pulse grid) auto-selects in
the derivatives picker even when a fresh `positionsStore.load()` returns empty due to 502.
