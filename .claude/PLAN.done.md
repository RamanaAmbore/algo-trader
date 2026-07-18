# Plan: Universal Single-Row Card Header + Activity Consolidation + Derivatives Grid Fixes

## Context

Three categories of fixes: (1) All cards should have ONE header row — title | separator | scrollable middle (tabs or any elements) | right (filters + buttons); currently activity cards show two rows. (2) Derivatives page inconsistencies — Legs symbol appears as an amber chip in the header, Legs grid rows lack alternating backgrounds, Snapshot "all accounts" is a read-only label not a selector. (3) Ramana bio colours on the showcase page should match the section chip pattern.

---

## Task Summary

### A — Universal card header (one row)

**`frontend/src/lib/CardHeader.svelte`**
- Add `{#if middle}<span class="ch-sep" aria-hidden="true"></span>{/if}` between `.ch-left` and `.ch-middle`
- CSS: `.ch-sep { width: 1px; align-self: stretch; background: rgba(255,255,255,0.10); flex-shrink: 0; margin: 0.15rem 0; }`
- `.ch-middle` already has `overflow-x: auto; min-width: 0` — any overflow element (not just tabs) scrolls

**`frontend/src/lib/CardControls.svelte`**  
- Reorder buttons to canonical (CLAUDE.md): Search → Collapse → Fullscreen → DefaultSize → Download  
- Refresh stays first (conditional: `onRefresh && (isFullscreen || refreshAlwaysVisible)`)
- New order: `RefreshButton? · GridSearchButton · CollapseButton · FullscreenButton · DefaultSizeButton · GridDownloadButton`

**`frontend/src/lib/LogPanel.svelte`** — biggest change: absorb card chrome into ONE row
- Add new props: `label = ''`, `isCollapsed = $bindable(false)`, `isFullscreen = $bindable(false)`, `onRefresh = null`, `refreshLoading = $bindable(false)`, `onDownload = null`, `cardId = ''`, `onClose = null`
- Make `levelFilter = $bindable('all')` (was one-way prop)
- Import `ActivityHeaderFilters`
- Compute `_showAccountFilter`/`_showLevelFilter` from `logTab` internally
- NEW `.log-tab-row` layout:
  ```
  [lp-label, if context!=='page' && label] [lp-sep] [lp-tab-strip-wrap flex:1 1 0 min-w:0] [ActivityHeaderFilters] [lp-card-btns]
  ```
- `lp-tab-strip-wrap` replaces bare `<AlgoTabs>` — bounded flex container ensures tabs/any content scrolls, never pushes buttons
- Remove `margin-left: auto` from `.lp-tabrow-acct` and `.lp-card-btns` (wrapper takes all space)
- Card buttons (when `context !== 'page'`): add CollapseButton bound to `isCollapsed` + FullscreenButton bound to `isFullscreen` before Download
- Button order in `.lp-card-btns`: Search → Expand(internal height) → Collapse(card) → Fullscreen(card) → Download
- In `context === 'modal'`: show label with BellIcon prefix; replace Fullscreen with close button (from `onClose`); add gradient bg to `.log-tab-row`
- In `context === 'page'`: no label, no sep, no Collapse/Fullscreen buttons
- `.lp-label` uses `ch-title`-equivalent styling: amber, 0.6rem, 700 weight, uppercase, letter-spacing — NO left bar, NO chip chrome

**`frontend/src/lib/ActivityLogSurface.svelte`**
- Add passthrough props: `label`, `isCollapsed = $bindable(false)`, `isFullscreen = $bindable(false)`, `onRefresh`, `refreshLoading = $bindable(false)`, `onDownload`, `cardId`, `onClose`
- Change `levelFilter` from one-way to `$bindable`
- Pass all to LogPanel via `bind:` where applicable

**`frontend/src/lib/ActivityLogModal.svelte`**
- Remove `.alm-header` div (title + filters + close) — LogPanel renders this in modal context
- Pass `onClose` to ActivityLogSurface
- Remove `ActivityHeaderFilters` import, `_showAccountFilter`/`_showLevelFilter`/`_activeTab` state

**`frontend/src/routes/(algo)/orders/+page.svelte`**
- Remove the `<CardHeader>` wrapper around the activity section
- Pass to ActivityLogSurface: `label="ACTIVITY"`, `cardId="orders-activity"`, `onRefresh={loadOrders}`, `bind:isCollapsed={_colActivity}`, `bind:isFullscreen={_fsActivity}`
- Remove `ActivityHeaderFilters` import and usage in this section, `_showAccountFilter`/`_showLevelFilter`/`_actActiveTab`

**`frontend/src/routes/(algo)/dashboard/+page.svelte`**
- Remove `.row3-header` div and its CSS rule (`.row3-header`)
- Replace with ActivityLogSurface props: `label="ACTIVITY"`, `cardId="dash-activity"`, `onRefresh={_refreshAll}`, `bind:isCollapsed={_colActivity}`, `bind:isFullscreen={_fsActivity}`
- Remove `ActivityHeaderFilters`/`CardControls` imports for activity section, remove associated state

**`frontend/src/routes/(algo)/activity/+page.svelte`**
- Remove `ActivityHeaderFilters` from page header
- Remove `_showAccountFilter`/`_showLevelFilter`/`_activeTab` state
- `bind:accountFilter` and `bind:levelFilter` on ActivityLogSurface remain (activityStore persistence)
- Page header retains: BellIcon + Activity + timestamp + spacer + Refresh + PageHeaderActions

---

### B — Derivatives page fixes

**`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

**B1 — Legs header chip → plain label (line ~5694)**
Replace `.legs-underlying-chip` chip styling with plain text matching `ch-title` conventions:
```css
/* BEFORE */
.legs-underlying-chip {
  padding: 0.15rem 0.5rem; border-radius: 3px;
  background: var(--algo-amber-bg-strong);
  border: 1px solid var(--algo-amber-border);
  color: var(--c-action); font-size: var(--fs-lg); font-weight: 800;
}
/* AFTER */
.legs-underlying-chip {
  color: var(--c-action, #fbbf24);
  font-size: var(--fs-sm, 0.6rem);
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
}
```

**B2 — Snapshot "all accounts" → AccountMultiSelect (line ~4449)**
Replace `<span class="byund-scope">` with an interactive `<AccountMultiSelect>` component:
```svelte
<!-- BEFORE -->
<span class="byund-scope" title="...">
  {#if selectedAccounts.length === 0}all accounts{:else}{selectedAccounts.join(' · ')}{/if}
</span>

<!-- AFTER -->
<AccountMultiSelect
  bind:value={selectedAccounts}
  options={accountChoices.map(a => ({ value: a, label: a }))}
  placeholder="All accounts"
  ariaLabel="Filter Snapshot by broker account" />
```
Remove `.byund-scope` CSS rule (lines ~5586-5598).  
`AccountMultiSelect` import should already exist on the derivatives page — if not, add it from `$lib/AccountMultiSelect.svelte`.

**B3 — Legs grid alternating row backgrounds**
Add in `CandidateLegRow.svelte` style block OR in derivatives/+page.svelte parent CSS:
```css
/* Match .byund-row:nth-of-type(odd) pattern and ag-theme-algo */
:global(.cand-rows-list > :nth-child(odd) > .cand-row) {
  background-color: rgba(13,22,42,0.30);
}
```
Check the actual grid container selector to confirm the right parent — the existing `.byund-row:nth-of-type(odd) > span` pattern (line 5516) is the reference.

---

### C — Showcase bio colours

**`frontend/src/routes/(algo)/showcase/+page.svelte`**  
Update `.show-attribution` (Ramana bio panel) to use the same `color-mix` pattern as `.show-card-tag`:
```css
/* BEFORE */
background: rgba(251, 191, 36, 0.10);
border: 1px solid rgba(251, 191, 36, 0.40);

/* AFTER */
background: color-mix(in srgb, var(--c-action, #fbbf24) 12%, transparent);
border: 1px solid color-mix(in srgb, var(--c-action, #fbbf24) 35%, transparent);
```

---

## Agents

- **frontend**: All changes in sections A, B, C — 9 Svelte files
- **playwright**: Update `e2e/activity-panel.spec.ts` for one-row header (no separate CardHeader above LogPanel; filters inside LogPanel's single row; tab strip selectors unchanged)
- **backend**: skip
- **broker**: skip
- **doc**: skip
- **backend-test**: skip

---

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes — update `e2e/activity-panel.spec.ts`

---

## Commit message

```
refactor(ui): single-row card header, activity consolidation, derivatives grid fixes
```

## Done when

1. Every activity surface (orders, dashboard, modal, /activity page) shows ONE header row: label | separator | scrollable middle | filters | buttons — no second tab row anywhere.
2. Middle content (tabs, filters, any elements) scrolls within its bounded flex area; never pushes the card button group.
3. Dashboard activity label has NO left amber bar — plain `ch-title`-equivalent styling matching orders page.
4. CardHeader shows a `1px` vertical separator between left zone and middle content (Gainers/Losers cards); absent when no `middle` snippet.
5. CardControls button order is canonical everywhere: Search → Collapse → Fullscreen → DefaultSize → Download.
6. Legs header "BHEL" text is plain amber label, not a chip (no background/border/padding).
7. Legs grid rows have alternating `rgba(13,22,42,0.30)` row background matching Snapshot and ag-theme-algo.
8. Snapshot "all accounts" is an interactive `AccountMultiSelect` dropdown.
9. Showcase page Ramana bio panel uses `color-mix` 12%/35% pattern matching section chips.
10. `svelte-check` 0 errors; Playwright activity-panel spec passes.
