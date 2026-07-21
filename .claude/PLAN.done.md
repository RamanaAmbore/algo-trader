# Plan: UI fullscreen polish + AlgoTimestamp fixes

## Task

**Group A — AlgoTimestamp bugs:**

1. **Mobile toggle broken** — `onclick` on `<span>` is unreliable on iOS Safari. No `ontouchend`
   handler. Fix: convert root element to `<button type="button">` (handles touch natively and
   removes the need for explicit `role="button"` and `tabindex`).

2. **Desktop refresh timestamp delay** — `lastRefreshAt` is not a true SSOT. Four writers set it
   at inconsistent points:
   - `MarketPulse.svelte:2699` — after `buildQuoteMaps()` (50-300ms after data arrives)
   - `execution/+page.svelte:79` — BEFORE the async fetch starts (wrong direction)
   - `dashboard/+page.svelte:1200` — after hero-batch only, before funds completes
   - `RefreshButton.svelte:373` — on `loading` false transition (correct pattern)
   Fix: Consolidate. `RefreshButton` is the canonical setter. Remove the three ad-hoc writes
   and rely on `RefreshButton`'s `loading` transition for all pages that use it. For
   MarketPulse (which has no RefreshButton in the header), move `lastRefreshAt.set()` to
   immediately after the parallel store loads settle — before `buildQuoteMaps()`.

**Group B — Fullscreen/modal UI fixes (reusable-first):**

3. **SymbolPanel — chart button**: add to header; ChartModal opens on top (DOM order handles z).
4. **SymbolPanel — chase L/M/H position**: move from header to picker row.
5. **Fullscreen refresh gap** — 6 cards missing `onRefresh` wiring.
6. **Fullscreen height fill** — SimulatorPanel/ReplayPanel charts don't expand; Order Entry
   and Derivatives Legs lack rules.
7. **DefaultSizeButton missing from LogPanel** — raw `<FullscreenButton>` with no paired restore.

**Reusability strategy for Group B:**
- DefaultSizeButton and RefreshButton are already reusable components — just need correct wiring.
- For fullscreen height fill: add a **canonical utility class** `fs-content-fill` to `app.css`
  instead of per-component one-off CSS rules. Components set `--fs-chrome-h` custom property to
  account for their own header height; the utility computes `height: calc(100vh - var(--fs-chrome-h, 8rem))`.
  SimulatorPanel, ReplayPanel, Order Entry, and Derivatives Legs all adopt this single pattern.

## Agents

- frontend: Implement all fixes below.

  ### Group A — AlgoTimestamp

  **A1 — `frontend/src/lib/AlgoTimestamp.svelte`: fix mobile toggle + stuck-blank guard**

  Root cause: `_toggle()` has `if (_refreshTs)` guard — on Android, if `lastRefreshAt`
  is still 0 (page hasn't finished first load), `_refreshTs` is null and the guard exits
  immediately. The browser's tap ripple fires but Svelte state never changes.

  Fix 1 — convert root `<span>` to `<button type="button">` for reliable tap on all
  platforms. Remove `role="button"` and `tabindex="0"` (native button provides these).
  Reset button default styles in `<style>`: `background: none; border: none; padding: 0;
  font: inherit; cursor: pointer;` on `.ats-group`.

  Fix 2 — drop the `if (_refreshTs)` guard in `_toggle()` so the toggle always flips
  `_showRefresh`, even when no refresh time is available yet. When `_showRefresh=true`
  but `_refreshTs` is null, `.ats-now` hides but nothing else shows — blank display.

  Fix 3 — add auto-reset effect to prevent the blank stuck state:
  ```javascript
  $effect(() => { if (!_refreshTs && _showRefresh) _showRefresh = false; });
  ```
  This reverts to current-time view automatically when refresh time is unavailable.

  **A2 — `frontend/src/lib/stores.js`: document `lastRefreshAt` contract**
  Add a comment above `export const lastRefreshAt = writable(0)` clarifying:
  "Set by RefreshButton on loading-false transition. Other writers must set immediately after
  data arrives — not after processing." (one-line comment, no over-documentation.)

  **A3 — `frontend/src/lib/MarketPulse.svelte:2699`: move timestamp to after data arrives**
  Currently `lastRefreshAt.set(pulseLastUpdate)` is called at line 2699, after `buildQuoteMaps()`.
  Move it to immediately after `await Promise.allSettled([...])` resolves (i.e., after the
  parallel store loads at line 2590, before the quote processing). Keep `pulseLastUpdate`
  assignment at its new earlier position.

  **A4 — `frontend/src/routes/(algo)/admin/execution/+page.svelte:79`: fix wrong-direction set**
  Remove `lastRefreshAt.set(Date.now())` that fires BEFORE `loadPanel(tab)` starts.
  This page uses a RefreshButton; let it handle the timestamp on loading-false transition.

  **A5 — `frontend/src/routes/(algo)/dashboard/+page.svelte:1200`: verify timing**
  Read the `loadHero()` function around line 1200. If `lastRefreshAt.set()` is called before
  `Promise.all([fundsStore.load(), _fetchNifty()])` completes, move it to after both resolve,
  OR leave as-is and note if RefreshButton already handles it for the dashboard too.

  ### Group B — Fullscreen/modal UI

  **B0 — `frontend/src/app.css`: add reusable `fs-content-fill` utility**
  Add after the existing `.fs-card-on` rules:
  ```css
  /* Content inside a fullscreen card fills available height.
     Set --fs-chrome-h on the card section to account for header height. */
  .fs-card-on .fs-content-fill {
    height: calc(100vh - 4rem - var(--fs-chrome-h, 6rem));
    overflow-y: auto;
  }
  ```
  (4rem = 2rem top + 2rem bottom inset from `.fs-card-on { inset: 2rem }`)

  **B1 — `frontend/src/lib/SymbolPanel.svelte`: chart button + chase position**
  
  Chart button: in `{#snippet right()}` (line ~1978), add before the `.oes-header-cluster` span:
  ```svelte
  {#if !inline && _localSymbol}
    <button type="button" class="oes-chart-btn" title="Chart — {_localSymbol}"
            onclick={() => _chartModalOpen = true}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
      </svg>
    </button>
  {/if}
  ```
  CSS class `.oes-chart-btn` already exists at line 3223. ChartModal already portals at same
  z-index; DOM mount order puts it on top.

  Chase L/M/H: remove `{#if _chaseEnabled}...ChaseAggPicker...{/if}` from `{#snippet right()}`
  (lines 1990-1993). Add in the `.oes-picker` div AFTER the exchange chip (after ~line 2065):
  ```svelte
  {#if !inline && _chaseEnabled}
    <span class="oes-common-chase-label on" title="Chase is active">CHASE</span>
    <ChaseAggPicker value={_sharedChaseAgg} onChange={_setSharedChaseAgg} variant="panel" />
  {/if}
  ```

  **B2 — Fullscreen refresh gap: wire onRefresh + refreshLoading to 6 cards**

  RefreshButton is the SAME component in page header and card controls. For the spinner,
  animation, and tooltip to work identically, `loading` must be properly bound. Each card
  needs a local `_refreshing = $state(false)` and an async wrapper around the fetch.

  Pattern for each card:
  ```svelte
  let _refreshingX = $state(false);
  async function _refreshX() {
    _refreshingX = true;
    try { await loadAll({ fresh: true }); } finally { _refreshingX = false; }
  }
  // CardHeader:
  onRefresh={_refreshX} bind:refreshLoading={_refreshingX}
  ```

  `frontend/src/lib/PerformancePage.svelte` — `loadAll` at line 1022:
  Add one `_perfRefreshing = $state(false)` + async wrapper. Apply to CardHeaders at
  lines 1339, 1353, 1371, 1382 (all 4 share the same refresh function — one state var).

  `frontend/src/lib/execution/SimulatorPanel.svelte` — `loadAll` at line 416:
  Add `_simRefreshing = $state(false)` + async wrapper. Apply to CardHeader at line 968.

  `frontend/src/lib/execution/ReplayPanel.svelte` — `load` at line 70:
  Add `_replayRefreshing = $state(false)` + async wrapper. Apply to CardHeader at line 359.

  **B3 — Fullscreen height fill: use `fs-content-fill` utility**

  `frontend/src/lib/execution/SimulatorPanel.svelte`:
  Wrap the `<div class="sim-charts">` in a container that uses the utility:
  ```svelte
  <div class="sim-charts fs-content-fill" style="--fs-chrome-h: 5rem;">
  ```
  Also set `display: flex; flex-direction: column; gap: 0.5rem` on `.sim-charts` in the
  component style, and add `:global(.sim-card.fs-card-on) .sim-charts :global(.price-chart)
  { flex: 1 1 0; min-height: 200px; }` so charts divide the space equally.

  `frontend/src/lib/execution/ReplayPanel.svelte`: Same pattern for `.replay-charts`.

  `frontend/src/routes/(algo)/orders/+page.svelte`:
  Find the SymbolPanel wrapper in the `bucket-card-entry` section. Add `fs-content-fill` class
  (or equivalent CSS rule) so SymbolPanel fills height when the bucket goes fullscreen.
  Check actual class on the SymbolPanel wrapper and apply the utility there.

  `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:
  Find the Legs grid container inside the legs `fs-card-on` card. Apply `fs-content-fill`
  to the ag-Grid wrapper div. Set `--fs-chrome-h` to match the legs header height (~3.5rem).
  If the grid container already fills via flex, confirm visually and skip if correct.

  **B4 — `frontend/src/lib/LogPanel.svelte:1478`: add DefaultSizeButton**
  Import `DefaultSizeButton` at the top of the file (alongside the existing FullscreenButton import).
  Replace raw `<FullscreenButton bind:isFullscreen />` with the canonical CardControls pair:
  ```svelte
  {#if !isFullscreen}
    <FullscreenButton bind:isFullscreen label={label} />
  {:else}
    <DefaultSizeButton bind:isFullscreen bind:isCollapsed label={label} />
  {/if}
  ```
  Verify `isCollapsed` is accessible in this scope (LogPanel should already have it as a prop/state).

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

fix(ui): AlgoTimestamp mobile toggle + lastRefreshAt SSOT + fullscreen polish

Group A — AlgoTimestamp:
- Convert root span to button for reliable iOS tap handling
- Move lastRefreshAt.set() in MarketPulse to after data arrives (not after buildQuoteMaps)
- Remove premature lastRefreshAt.set() in execution/+page before async fetch

Group B — Fullscreen UI (reusable-first):
- Add fs-content-fill utility class in app.css (avoids per-component one-offs)
- SymbolPanel: chart button in header + CHASE/L/M/H moved to picker row
- Wire onRefresh to 6 fullscreen cards (PerformancePage ×4, SimulatorPanel, ReplayPanel)
- SimulatorPanel + ReplayPanel: PriceCharts fill fullscreen via fs-content-fill
- LogPanel: pair FullscreenButton with DefaultSizeButton (canonical restore button)

## Done when

- AlgoTimestamp tapping works on iOS mobile (toggles between current time and refresh time)
- Refresh timestamp appears promptly on desktop after data loads (no post-processing lag)
- execution/+page no longer sets lastRefreshAt before fetch completes
- Chart button in order modal header opens ChartModal; closing returns to modal
- CHASE + L/M/H appear in picker row (not header) in order modal
- RefreshButton shows in fullscreen for all 6 previously-gap cards
- Charts in SimulatorPanel/ReplayPanel expand to fill fullscreen height
- Activity card fullscreen shows DefaultSizeButton restore button
- svelte-check 0 errors
