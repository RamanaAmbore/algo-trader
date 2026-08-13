# Plan: derivatives picker — add pinned + watchlist virtual underlyings

## Context
The underlying picker currently has 4 tiers: options positions → futures positions → holdings → POPULAR_UNDERLYINGS (NIFTY/BANKNIFTY/…).

When the operator has no open position (pre-market, weekend, broker 502), the picker falls back to NIFTY even if the operator has CRUDEOIL in their watchlist. The fix extends the picker with pinned watchlist roots (Tier 4) and non-pinned watchlist roots (Tier 5), so the operator's watchlist acts as a source of intent even when positions are absent.

`_watchlistSyms` is currently a dead state var (set in `loadDefaultWatchlist`, never read by the picker). This plan wires it correctly and splits it by tier.

## New tier order
1. Options positions (`_rootsWithOptions`)  — hint: 'options'
2. Futures positions (`_rootsWithFuturesOnly`) — hint: 'futures'
3. Holdings (equity) — hint: 'holdings'
4. **NEW** Pinned watchlist virtual underlyings — hint: 'pinned'
5. **NEW** Non-pinned watchlist virtual underlyings — hint: 'watchlist'
6. POPULAR_UNDERLYINGS — hint: 'popular' (final fallback)

"Virtual underlying" = `decomposeSymbol(sym).root` for F&O symbols; bare `tradingsymbol` for equity (e.g. RELIANCE is its own underlying). Filter: `getOptionUnderlyingLot(root) > 0` to exclude roots the broker has no listed derivatives for.

## Agents
- frontend: Two files to change:

  **File 1 — `frontend/src/lib/data/watchlistSymbols.js`**

  Update `_cache` type annotation to include `pinnedSyms: string[]` and `regularSyms: string[]`.

  In `loadWatchlistSymbols()`, after building `pinnedFirst = [...pinnedLists, ...regularLists]` and fetching `details`, split into:
  ```js
  const pinnedDetails  = details.slice(0, pinnedLists.length);
  const regularDetails = details.slice(pinnedLists.length);
  ```
  Build separate deduped arrays:
  - `pinnedSyms` — from pinnedDetails only
  - `regularSyms` — from regularDetails, skipping already-seen symbols (i.e. not in pinnedSyms)
  - `syms` — union in pinned-first order (unchanged semantics for existing callers)

  Store all three in `_cache` and return them. Update the empty-fallback path to also include `pinnedSyms: [], regularSyms: []`.

  **File 2 — `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

  1. Add import: `import { loadWatchlistSymbols } from '$lib/data/watchlistSymbols.js';`

  2. Replace `let _watchlistSyms = $state([]);` with:
     ```js
     let _pinnedWatchlistRoots = $state(/** @type {string[]} */ ([]));
     let _regularWatchlistRoots = $state(/** @type {string[]} */ ([]));
     ```

  3. Add private helper (immediately after the new state vars):
     ```js
     /** @param {string[]} syms */
     function _extractFOUnderlyingRoots(syms) {
       const roots = new Set();
       for (const sym of syms) {
         const s = sym.toUpperCase().replace(/\s+/g, '');
         if (!s) continue;
         if (getOptionUnderlyingLot(s) > 0) { roots.add(s); continue; }
         const d = decomposeSymbol(s);
         if (d?.root && getOptionUnderlyingLot(d.root) > 0) roots.add(d.root);
       }
       return Array.from(roots).sort();
     }
     ```

  4. Refactor `loadDefaultWatchlist()`:
     - Replace the `fetchWatchlists()` + `fetchWatchlist(def.id)` double-call with a single `loadWatchlistSymbols()` call
     - Find the default list from `result.lists` (instead of a second `fetchWatchlists()` call)
     - Remove the `_watchlistSyms` assignment; add:
       ```js
       _pinnedWatchlistRoots  = _extractFOUnderlyingRoots(result.pinnedSyms);
       _regularWatchlistRoots = _extractFOUnderlyingRoots(result.regularSyms);
       ```

  5. In `underlyingOptionsForPicker` ($derived.by at line ~1445), insert after Tier 3 (holdings) and before Tier 4 (POPULAR_UNDERLYINGS):
     ```js
     // Tier 4 — Pinned watchlist virtual underlyings
     for (const u of _pinnedWatchlistRoots) {
       if (!u || seen.has(u)) continue;
       seen.add(u);
       out.push({ value: u, label: u, hint: 'pinned' });
     }
     // Tier 5 — Non-pinned watchlist virtual underlyings
     for (const u of _regularWatchlistRoots) {
       if (!u || seen.has(u)) continue;
       seen.add(u);
       out.push({ value: u, label: u, hint: 'watchlist' });
     }
     ```

  6. In the auto-select `$effect` (line ~1512), add after `void _positionsLoaded;`:
     ```js
     void _pinnedWatchlistRoots;
     void _regularWatchlistRoots;
     ```

  7. Update provisional NIFTY seed guard (line ~3830):
     ```js
     if (!selectedUnderlying && !(positionsStore.value?.length) && !(pulsePositionsStore.value?.length)
         && !_pinnedWatchlistRoots.length && !_regularWatchlistRoots.length) {
       selectedUnderlying = POPULAR_UNDERLYINGS[0];
     }
     ```

  The existing promote case (line ~1529) already handles the upgrade:
  `curIsPopular && opts[0]?.hint !== 'popular'` — 'pinned' and 'watchlist' are not 'popular', so once watchlist roots load and populate the picker, the auto-select re-fires and promotes NIFTY → first watchlist root automatically.

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Key files
- `frontend/src/lib/data/watchlistSymbols.js` — add `pinnedSyms`/`regularSyms` to cache/return
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — new tiers, refactored loader

## Reused utilities
- `loadWatchlistSymbols()` — `frontend/src/lib/data/watchlistSymbols.js:28` (already fetches + caches all lists split by pinned/global)
- `decomposeSymbol(sym).root` — `frontend/src/lib/data/decomposeSymbol.js:66`
- `getOptionUnderlyingLot(sym)` — already imported in derivatives page, used in `_hedgeOpportunities`

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(derivatives): add pinned + watchlist virtual underlyings to underlying picker

## Done when
`npx svelte-check` exits 0. The underlying picker shows CRUDEOIL (or any F&O-eligible root from pinned/non-pinned watchlists) in Tiers 4/5 even when the operator has no open positions. Positions still appear first.
