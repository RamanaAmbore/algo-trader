# Plan: Pulse/Public UI consistency — flash guard + icon colors + sort fix

## Context
Four related UI consistency issues discovered across Pulse and public pages:

1. **Pinned card flash (and other MarketPulse stores)** — `$derived(store.value ?? [])` snaps
   to `[]` when a DataStore reloads (store value transiently goes null). With ag-Grid's
   default `animateRows: true`, the empty→refill cycle makes rows fade in — card background
   shows through. Positions/Holdings already have the stale-while-revalidate bridge; three
   other stores and two persistent strip components do not.

2. **Public performance page icon colors** — CardControls icons on the cream-themed public
   layout use the algo-dark cyan (`#22d3ee`, `--c-info`) which is visually out-of-place on
   the light cream background. The correct token for the public scheme is champagne gold
   (`#c8a84b`). Fix is a CSS token override in `.pub-viewport` in the public layout.

3. **Derivatives page refresh animation** — Investigated. Derivatives uses NO ag-Grid and
   already uses the CardHeader spinner for loading state. No change needed.

4. **Pulse card header sort not responding** — `postSortGroups` in `pulseGridSetup.js`
   reorders all rows after every sort to cluster underlyings together. This nullifies any
   column sort the user clicked — from the user's perspective the sort "doesn't respond."
   All 6 bucket grids in MarketPulse are affected. Dashboard, PerformancePage, and
   Derivatives (no ag-Grid) are confirmed unaffected.

## Agents

### frontend agent task

Make all four changes below. Read each target file before editing.

---

**Change 1 — Stale-while-revalidate bridges in MarketPulse.svelte**
(`frontend/src/lib/MarketPulse.svelte`)

Apply the bridge pattern to three stores currently using `$derived(store.value ?? [])`:

- Line ~166: `const activeLists = $derived(activeListsStore.value ?? []);`
- Line ~565: `const movers = $derived(moversStore.value ?? []);`
- Line ~755: `const funds = $derived(fundsStore.value ?? []);`

Replace each with:
```js
let <name> = $state(<store>.value ?? []);
$effect(() => {
  const v = <store>.value;
  untrack(() => { if (v != null) <name> = v; });
});
```

Do NOT touch line ~757 (`sparklines` — feeds cell renderer, not rowData).
Do NOT touch positions/holdings bridge (lines ~191-200 — already correct).
`untrack` is already imported in this file.

---

**Change 2 — Stale-while-revalidate bridges in persistent strip components**
(`frontend/src/lib/PositionStrip.svelte` and `frontend/src/lib/NavBreakdown.svelte`)

In each file, find the `$derived(store.value ?? [])` bindings for positions/holdings/funds
stores (PositionStrip ~lines 27-29; NavBreakdown ~lines 62-66) and apply the same bridge.
Add `import { untrack } from 'svelte'` if not already imported.

---

**Change 3 — Public layout icon colors**
(`frontend/src/routes/(public)/+layout.svelte`)

In the `.pub-viewport` CSS rule, add these overrides so CardControls icons use champagne
gold instead of algo-dark cyan on the cream-themed public pages:
```css
--c-info: #c8a84b;
--algo-cyan-bg: rgba(200, 168, 75, 0.14);
--algo-cyan-border: rgba(200, 168, 75, 0.55);
--algo-cyan-bg-soft: rgba(200, 168, 75, 0.08);
--algo-cyan-text: #d4b85c;
```

---

**Change 4 — Pulse ag-Grid sort fix**
(`frontend/src/lib/data/pulseGridSetup.js`)

In the `postSortGroups` function, add an early-return guard at the very top that skips
the underlying-grouping reorder when the user has an active column sort:
```js
function postSortGroups(params) {
  // Respect user's column sort — skip underlying grouping when sort is active.
  if (params.api.getColumnState().some(col => col.sort != null)) return;
  // ... rest of existing logic unchanged ...
}
```

This means: no user sort active → groups by underlying (existing behaviour); user clicks
a column header → sort applies cleanly without being overridden by the grouping.

---

**Test requirement (mandatory)**
For every file you change or create, write or update at least one test:
- `frontend/src/lib/data/pulseGridSetup.js` change → add/update a Vitest test in
  `frontend/src/lib/__tests__/` covering the `postSortGroups` sort-active early-return
- `.svelte` changes → verify svelte-check passes 0 errors; add a Playwright smoke in
  `frontend/tests/` covering the changed behavior if a suitable spec exists
- No change ships without a corresponding test update.

## Agent list
- frontend: all four changes above
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
fix(ui): stale-while-revalidate for MarketPulse stores + public icon colors + pulse column sort

## Done when
- Pinned / Winners / Losers / Funds cards no longer flash on MarketPulse refresh
- PositionStrip and NavBreakdown retain prior values during store reload
- CardControls icons on public performance page show champagne gold (not cyan)
- Clicking a column header in any Pulse card applies the column sort cleanly
- svelte-check exits 0 errors
