---
# Plan: UI Polish Pass — Timestamps, Showcase, Activity Panel, Chart Modal, Demo Banner

## Context
Eight operator observations from a UI review session. Mix of visual polish (timestamps,
name, colors), functional bugs (collapse broken, chart dropdowns missing), and layout
fixes (demo banner position, filter alignment, chart modal single row on desktop).

---

## O1 — Dual-timezone timestamps on all pages

### What exists
- `nowStamp` store (stores.js) already produces `"Mon 20 Apr · 21:42 IST · 12:36 EDT"` — dual-tz format ✓
- `lastRefreshAt` writable store (epoch ms) + `formatDualTz()` helper exist in stores.js
- `algo-ts-group` toggle pattern already built on `/pulse` and `/dashboard` pages
- Page title bars are per-route — no centralized layout timestamp

### What needs to change
- **Format**: `formatDualTz(lastRefreshAt)` produces the same `"Mon D Mon · HH:MM IST · HH:MM EDT"` format
  as `nowStamp` — this is already correct; just need to display it
- **Desktop layout** in every page title bar:
  `[nowStamp]  |  [formatted lastRefreshAt]`
  Both always visible. Separator: `algo-ts-vsep` (existing class, `|` glyph).
- **Mobile**: one visible at a time; click to toggle between nowStamp and lastRefreshAt
  (same `_showLiveTs` state variable + `algo-ts-hidden` class pattern as pulse page)
- **Refresh timestamp source**: `lastRefreshAt` already gets set by RefreshButton + page
  load calls (MCX, positions, holdings, cash, margin all funnel through it). No new
  sourcing needed — just display it on all pages.
- **Pages to update**: All algo pages that have a timestamp in the title bar but DON'T
  yet have the two-stamp group. Already done: pulse, dashboard. Remaining: orders,
  positions, holdings, performance, activity, strategies, automation, lab, admin pages,
  showcase, charts — scan `grep -r "algo-ts" frontend/src/routes` to get the full list.

### Implementation
Agent: frontend
- Add `lastRefreshAt` import from stores where missing
- Add `_showLiveTs = $state(false)` local toggle where missing
- Replace bare `<span class="algo-ts">{$nowStamp}</span>` with full algo-ts-group:
  ```svelte
  <span class="algo-ts-group">
    <span class="algo-ts" class:algo-ts-hidden={_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs} ...>{$nowStamp}</span>
    <span class="algo-ts-vsep" aria-hidden="true">|</span>
    <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs} ...>
      {formatDualTz($lastRefreshAt)}
    </span>
  </span>
  ```
- Import `formatDualTz` from `$lib/stores` alongside `nowStamp`
- CSS (add to page-local style or ensure global): `.algo-ts-group`, `.algo-ts-vsep`,
  `@media (max-width:480px) .algo-ts-hidden { display:none !important }` — copy from pulse page
- On desktop (>480px): both spans visible, no hidden class applied → shows both side by side

---

## O2 — Name change + showcase contact colors

### O2a — "Ramana Ambore" → "Ramana R. Ambore" (6 frontend + 2 backend occurrences)
Files and lines:
1. `frontend/src/routes/(algo)/+layout.svelte:1354` — footer `<a>` text
2. `frontend/src/routes/(algo)/showcase/+page.svelte:248` — `<title>` tag
3. `frontend/src/routes/(algo)/showcase/+page.svelte:249` — meta description content
4. `frontend/src/routes/(public)/+page.svelte:46` — JSON-LD `"name"` field
5. `frontend/src/routes/(public)/+layout.svelte:205` — public footer `<a>` text
6. `frontend/src/routes/(public)/+layout.svelte:212` — public footer `<a>` text (duplicate)
7. `backend/config/frontend_config.yaml` — two changelog entries ("Ramana Ambore (Rambo)")

Agent: frontend for 1-6; doc agent for 7.

### O2b — Showcase contact button text color → match RISK ENGINE text
- RISK ENGINE text color: `color: var(--accent)` with `--accent: #7dd3fc` → computed `#7dd3fc`
- Contact buttons (`.show-contact-btn`) currently: `color: rgba(148, 163, 184, 0.95)` (muted slate)
- Fix: change `.show-contact-btn { color: #7dd3fc; }` in showcase page's `<style>` block
- Apply to icon SVGs too: `stroke: currentColor` (already used) will pick up the new color

---

## O3 — Activity card buttons: size/pattern parity + collapse/expand bug

### O3a — Button size + pattern: match holdings card
- Holdings card buttons (via CollapseButton.svelte): `1.4rem × 1.4rem`, `border-radius: 3px`,
  `border: 1px solid var(--algo-cyan-border)`, `background: var(--algo-cyan-bg)`
- LogPanel `.lp-card-btn`: `1.35rem × 1.35rem`, slate/muted border, no bg
- Fix: update LogPanel's `.lp-card-btn` CSS to `1.4rem × 1.4rem` and match the
  cyan border/bg tokens. Also align gap between buttons to match CardControls (0.3rem).
- Applies to all activity panel instances (orders, dashboard, /activity, modal)
  because they all render via LogPanel → `.lp-card-btns` → `.lp-card-btn`

### O3b — Collapse/expand non-functional (P1 bug)
**Root cause found**: `LogPanel.svelte` line 1527 — `.lp-body-wrap` div has no
`hidden={isCollapsed}` attribute. The `isCollapsed` prop is accepted and bound to
CollapseButton correctly, but the body never reacts to it.

Fix: add `hidden={isCollapsed}` to the `.lp-body-wrap` div in LogPanel.svelte.
```svelte
<!-- line 1527, change: -->
<div class="lp-body-wrap {_expanded ? 'lp-body-expanded' : ''}">
<!-- to: -->
<div class="lp-body-wrap {_expanded ? 'lp-body-expanded' : ''}" hidden={isCollapsed}>
```

Other cards (dashboard, positions, etc.) are wired correctly via `hidden={_colXxx}`.
Only LogPanel is broken. Verify by checking one more card (orders/holdings) after fix.

---

## O4 — Activity panel filter dropdowns: left-align

**Current state**: ActivityHeaderFilters (`.act-filters`) is placed AFTER the
`.lp-tab-strip-wrap` (flex:1) in LogPanel's header row. Because tab-strip-wrap is
flex:1 and takes all remaining space, the filters are pushed to the right of center.

**Fix**: Move ActivityHeaderFilters INSIDE `.lp-tab-strip-wrap`, before the tabs.
This makes the filters appear at the left edge of the middle zone, with tabs scrolling
to the right of them.

LogPanel.svelte header row change (schematic):
```
Before: [label] [sep] [lp-tab-strip-wrap: tabs] [ActivityHeaderFilters] [lp-card-btns]
After:  [label] [sep] [lp-tab-strip-wrap: [ActivityHeaderFilters] [tabs]] [lp-card-btns]
```

Move `<ActivityHeaderFilters .../>` inside `.lp-tab-strip-wrap` as first child.

---

## O5 — Chart modal: show account, symbol, candle dropdowns

**Root cause**: ChartModal passes `compact={true}` and `showHeader={false}` to
ChartWorkspace. `showHeader={false}` suppresses the `.cw-picker` row entirely — so
symbol type, symbol search, and chart type dropdowns never render in the modal.

**Fix in `ChartModal.svelte`**:
- Remove `showHeader={false}` (or change to `showHeader={true}` / omit the prop)
- The `.cw-picker` row (symbol type + symbol search + chart type) will now appear
- Also check if ChartModal has its own account selector prop — read the file and
  ensure any account/broker dropdown it manages is also visible within the modal panel

---

## O6 — Chart modal desktop: merge two rows into one

**Current**: Two rows in ChartWorkspace header:
- Row 1 (`.cw-picker`): symbol type Select + SymbolSearchInput + chart type Select
- Row 2 (`.cw-controls`): intraday toggle + date range pills + indicators MultiSelect + signals btn + reset zoom

**Desktop fix**: Wrap both rows in a single flex container on desktop:
```css
@media (min-width: 640px) {
  .cw-header-wrap {   /* new wrapper or existing parent */
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: 0.5rem;
  }
  .cw-picker, .cw-controls { flex-shrink: 0; }
}
```
Or: add `flex-direction: row` on the ChartWorkspace header container for `@media (min-width: 640px)`.

**Mobile**: no change — rows stack vertically as today.

---

## O7 — Demo mode banner: move to below NavStrip, above page content

**Current DOM order in `+layout.svelte`**:
```
<ImpersonationBanner />
<main class="algo-content">
  {#if isDemo ...}<div class="demo-banner">...</div>{/if}  ← inside main
  {@render children()}
</main>
```

**Fix**: Move demo banner OUTSIDE `<main>`, between `<ImpersonationBanner>` and `<main>`:
```svelte
<ImpersonationBanner />
{#if isDemo && !_demoBannerDismissed}
  <div class="demo-banner" role="status">
    ...
  </div>
{/if}
<main class="algo-content">
  {@render children()}
</main>
```

This places it in the layout chrome between NavStrip and the page content area,
without being inside `<main>`. Review `.demo-banner` CSS (width, margin, z-index)
to ensure it lays out correctly outside `<main>`.

---

## O8 — Chart page/modal: persist dropdown selections

**Currently persisted**: only overlays (localStorage `rbq.cache.chart-overlays.v1`).
**Needs persistence**: symbol, exchange, days (range), chart type (line/candle/area/plot).
Account selector is managed by ChartModal externally — persist separately.

**Implementation in `frontend/src/lib/data/chartStore.svelte.js`**:
- Add localStorage read/write for symbol, exchange, days, chartType using existing
  `readChartPref()` / `writeChartPref()` helpers
- New keys: `rbq.cache.chart-symbol.v1`, `rbq.cache.chart-exchange.v1`,
  `rbq.cache.chart-days.v1`, `rbq.cache.chart-type.v1`
- Hydrate on store init (call `readChartPref()` for each); persist on each setter call
- For ChartModal account selector: find where it's stored and add equivalent localStorage
  persistence (key: `rbq.cache.chart-account.v1`)

---

---

## O9 — Agent activity label divider not in sync with other card dividers

The vertical bar separator between the label and the tab strip in LogPanel's header
(`.lp-sep`) is visually inconsistent with the separator used in other card headers
(`.ch-sep` in CardHeader.svelte).

- **CardHeader `.ch-sep`**: `width:1px`, `align-self:stretch`, `background:rgba(255,255,255,0.10)`,
  `margin:0.15rem 0`, `flex-shrink:0`
- **LogPanel `.lp-sep`**: likely has different margin or opacity — needs to read and align
- Fix: update `.lp-sep` CSS in LogPanel.svelte to exactly match `.ch-sep` values so all
  card label|content separators look identical

---

## Agents
- frontend: O1, O2a (frontend files), O2b, O3a, O3b, O4, O5, O6, O7, O8
- doc: O2a — `backend/config/frontend_config.yaml` name update
- backend: skip
- broker: skip
- backend-test: skip
- playwright: add/update specs — collapse/expand works on activity card (O3b);
  chart modal shows dropdowns (O5); demo banner position (O7)

## Tests
- pytest: no
- svelte-check: yes — 0 errors required
- playwright: yes — targeted checks for O3b, O5, O7

## Commit message
fix(ui): timestamp dual-tz on all pages, collapse/expand bug, chart modal dropdowns, demo banner position, activity filter alignment, showcase colors, name update, activity divider sync

## Done when
- All algo pages show `[nowStamp] | [lastRefreshAt]`; mobile toggles on click
- "Ramana R. Ambore" everywhere in footer + showcase + JSON-LD + config
- Showcase contact button text is `#7dd3fc` (sky blue) matching RISK ENGINE tag
- Activity card buttons are 1.4rem × 1.4rem, cyan pattern, matching holdings card
- Collapse/expand buttons function on all activity cards across all pages and modals
- Activity panel "All Accounts" / "All Error Types" dropdowns are left-aligned
- Chart modal shows symbol type, symbol search, and chart type dropdowns
- Chart modal desktop: one row for all controls; mobile unchanged
- Demo mode banner sits between NavStrip chrome and page content (outside `<main>`)
- Chart page/modal remembers last symbol, range, chart type, account across opens
- Activity label|tabs divider (`.lp-sep`) visually matches CardHeader `.ch-sep` (same width, color, margin)
- svelte-check: 0 errors
