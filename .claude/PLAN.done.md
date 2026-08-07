# Plan: Fix public performance page tab regressions + holdings day-% formula

## Task
Three bugs introduced or exposed by commit e11e4562 on the public `/performance` page:

1. **Tab alignment regression** — Positions/Holdings tab strip is not left-aligned with the
   Nav/Funds tab strip.  Both strips are `<div class="...-tabs mb-2">` wrappers but may have
   picked up different layout properties when `GridDownloadButton` (which had `margin-left:auto`)
   was removed from `funds-nav-tabs` in that commit.

2. **Tab click regression** — Clicking Positions / Holdings tabs doesn't switch the visible
   section.  Root cause: `switchTab()` calls `goto()` (SvelteKit router) which may remount or
   re-navigate the public page (prerender=false, ssr=false), resetting `activeTab` back to its
   URL-derived initial value before the `class:hidden` reactive update lands.
   Fix: replace `goto()` with `history.replaceState()` to update the URL without touching the
   router.

3. **Holdings day-% formula bug** — SIEMENS and WAAREEENER showing >20% day return in the
   snapshot holdings path.  In `holdings.py:_build_snapshot_row()` (line 106),
   `close_notional = abs(ltp_f * qty_i)` uses the snapshot LTP as denominator instead of
   yesterday's close price.  Kite's broker-provided `day_change_percentage` is available in
   the raw holdings response and should be stored in the snapshot and used directly, avoiding
   the recomputation entirely.

## Agents

- backend: skip
- frontend: Fix `PerformancePage.svelte` — two changes:
  (a) **Alignment**: Audit the CSS for `.funds-nav-tabs` and `.tabs-row`; ensure both divs
      render identically (same display, padding, margin).  If container structure differs
      (e.g. wrapping flex parent), add matching CSS so both strips share the same left edge.
  (b) **Click regression**: In `switchTab(id)` (line 212) replace the `goto()` call with
      `history.replaceState(null, '', url.toString())` so the SvelteKit router is bypassed
      and `activeTab` isn't reset by a navigation event.
      ```js
      function switchTab(id) {
        activeTab = id;
        const url = new URL(window.location.href);
        url.searchParams.set('tab', id);
        history.replaceState(null, '', url.toString());
      }
      ```
  File: `frontend/src/lib/PerformancePage.svelte`
- broker: skip
- doc: skip
- backend-test: Fix holdings day-% formula:
  (a) Read `backend/api/routes/holdings.py` snapshot path fully (lines 70–130) and read the
      DB persistence code that stores holding snapshots to identify the exact table/columns.
  (b) Check whether `day_change_percentage` (Kite-provided) is stored in the snapshot.
      - If YES: use it directly in `_build_snapshot_row()` instead of recomputing; set
        `close_price=ltp_f` (unchanged) but `day_change_percentage=stored_kite_pct`.
      - If NO: store it when writing the snapshot, then read it back in `_build_snapshot_row()`.
        Add a column `day_change_pct NUMERIC` to the holdings snapshot table (or use
        `day_pnl / |avg_cost × qty| × 100` as the correct fallback instead of LTP).
  (c) Write a pytest in `backend/tests/broker/` or `backend/tests/` covering:
      - snapshot row correctly uses close-based % (not LTP-based %)
      - zero-qty / zero-close guard paths still produce 0.0
  Files: `backend/api/routes/holdings.py`, holdings snapshot persistence layer (locate via grep)
- playwright: Add e2e spec for public performance page in `frontend/tests/`:
  - Load `/performance`
  - Assert initial visible section is Positions (Holdings section hidden)
  - Click Holdings tab → assert Holdings section visible, Positions hidden
  - Click Positions tab → assert Positions section visible, Holdings hidden
  - Assert both tab strips are left-aligned (left bounding box within 2px of each other)
  File: `frontend/tests/public-performance-tabs.spec.js`

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
fix(performance): replace goto() with history.replaceState in switchTab, align tab strips, fix holdings snapshot day-% denominator

## Done when
- Clicking Holdings tab on /performance shows holdings sections and hides positions sections (and vice-versa)
- Both tab strips are visually left-aligned
- SIEMENS/WAAREEENER day-% in snapshot holdings uses correct denominator (close-price or broker-provided value), not LTP
- Playwright spec passes for tab switching + alignment
- pytest passes for snapshot day-% formula
- svelte-check 0 errors
