# Plan: Card Header Unification — LogPanel + Spacing

## Context

`CardHeader` already ships the correct layout:
- **Left zone** — `flex-shrink: 0` — label + refresh indicator, never pushed off-screen
- **Middle zone** — `flex: 1 1 0; min-width: 0; overflow-x: auto; scrollbar-width: none` — tabs / chips / dropdowns scroll if they overflow, never hide the right buttons
- **Right zone** — `flex-shrink: 0` — CardControls buttons (Refresh · Search · Download · Collapse · Fullscreen)
- **Auto-fullscreen** — ResizeObserver + ag-Grid row check already wired via `detectOverflow` prop

`LogPanel` duplicates this entire layout with its own flat `lp-label + lp-sep + lp-tab-strip-wrap + lp-card-btns` row, bypassing CardHeader entirely. The result: different collapse icon (four-arrows vs chevron), different button sizing, no overflow scroll guarantee, and download button silently no-ops on the news tab with no feedback.

---

## Phase 1 — Extend CardHeader: add `hideFullscreen` prop

**File:** `frontend/src/lib/CardHeader.svelte`

Add one prop to forward directly to CardControls so callers that supply their own fullscreen behaviour (e.g. open-modal instead of card-portal) can suppress the standard FullscreenButton/DefaultSizeButton pair:

```js
hideFullscreen = false,   // new prop
```

Pass to CardControls:
```svelte
hideFullscreen={hideFullscreen || (detectOverflow && !_hasOverflow && !isFullscreen)}
```

That's the only change to CardHeader. CardControls needs no change — it already accepts `hideFullscreen`.

---

## Phase 2 — Migrate LogPanel to CardHeader

**File:** `frontend/src/lib/LogPanel.svelte`

### 2a. Add CardHeader import (already has CollapseButton, GridDownloadButton)
```js
import CardHeader from '$lib/CardHeader.svelte';
```

### 2b. Replace the label-branch button row

Remove the entire `{#if label}` block at lines 1419–1505 that renders `.lp-label`, `.lp-sep`, `.lp-tab-strip-wrap`, and `.lp-card-btns`.

Replace with a `<CardHeader>` usage:

```svelte
{#if label}
  <CardHeader
    title={label}
    {cardId}
    {onRefresh}
    bind:isCollapsed
    bind:refreshLoading
    showSearch={true}
    onDownload={logTab === 'news' ? null : (onDownload ?? _downloadCsv)}
    hideFullscreen={true}
  >
    {#snippet left()}
      {#if context === 'modal'}
        <BellIcon width="12" height="12" class="lp-label-icon" />
      {/if}
    {/snippet}
    {#snippet middle()}
      <AlgoTabs
        tabs={VISIBLE_TABS.map(([id, lbl]) => ({ id, label: lbl }))}
        bind:value={logTab}
        onChange={onTabChange}
        compact={true}
      />
      <ActivityHeaderFilters
        bind:accountFilter={_internalAccountFilter}
        bind:levelFilter
        availableAccounts={_availableAccounts}
        showAccountFilter={_showAccountFilter}
        showLevelFilter={_showLevelFilter} />
    {/snippet}
    {#snippet right()}
      {#if context === 'modal'}
        <button type="button" class="lp-close-btn"
                aria-label="Close activity log"
                onclick={() => onClose?.()}>×</button>
      {:else}
        <button type="button" class="lp-fs-btn"
                title="Open fullscreen"
                aria-label="Open fullscreen"
                onclick={() => openActivityModal()}>
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M3 3h4M3 3v4M13 3h-4M13 3v4M3 13h4M3 13v-4M13 13h-4M13 13v-4"
                  stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
        </button>
      {/if}
    {/snippet}
  </CardHeader>
{/if}
```

**Key wiring decisions:**
- `onDownload={logTab === 'news' ? null : ...}` — `GridDownloadButton` already auto-hides when `onClick=null`, so download button disappears on news tab (cleaner than disabled)
- `hideFullscreen={true}` — suppresses CardControls' standard FullscreenButton; LogPanel's own modal-open button sits in the right snippet instead
- `showSearch={true}` — `GridSearchButton` in CardControls binds `filter` which LogPanel can wire to `_searchQuery` via `bind:filter={_searchQuery}` on CardHeader
- Collapse uses `CollapseButton` from CardControls — chevron icon, localStorage persistence via `cardId`, matches every other card

### 2c. Remove old CSS
Delete `.lp-label`, `.lp-sep`, `.lp-tab-strip-wrap`, `.lp-card-btns`, `.lp-card-btn`, `.lp-card-btn-on`, `.lp-card-btn:hover`, `.canonical-card-btn-group` scoped styles. Add minimal styles for `.lp-close-btn` and `.lp-fs-btn` (match `cc-btn` size/colour: `1.4rem × 1.4rem`, cyan-400 border, 3px radius).

### 2d. Keep legacy path unchanged
The `{:else}` branch (`lp-card-btns-legacy`, lines 1506–1560+) stays until all direct non-label LogPanel mounts are audited. Mark it `<!-- DEPRECATED: remove after all mounts confirmed label-bearing -->`.

### 2e. Wire GridSearchButton filter to _searchQuery
CardHeader's `filter` bindable flows into `GridSearchButton`. In LogPanel, bind it: `bind:filter={_searchQuery}`. The existing `_searchOpen` local state can be dropped from the label-branch (GridSearchButton manages open/close internally).

---

## Phase 3 — Vertical spacing audit

**Files:** all `bucket-card` page files

Canonical values:
- Between sibling cards within a section: `gap: 0.6rem` (if flex/grid container) or `margin-top: 0.6rem` on second card
- Between sections: `margin-bottom: 0.75rem` on the outgoing section

Audit these pages and normalize any outliers:
- `routes/(algo)/dashboard/+page.svelte`
- `routes/(algo)/performance/+page.svelte`
- `routes/(algo)/orders/+page.svelte`
- `routes/(algo)/admin/derivatives/+page.svelte`
- `routes/(algo)/automation/templates/+page.svelte`
- `routes/(algo)/activity/+page.svelte`

If a global `.bucket-card + .bucket-card { margin-top: 0.6rem }` rule in `app.css` can replace all the per-page overrides cleanly, add it there and remove the redundant per-page values.

---

## Agents

- frontend: Implement Phases 1–3. One agent — all files touch the same layout layer, no cross-layer deps.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add/update spec — (1) LogPanel search filter works via GridSearchButton; (2) download button hidden on news tab, appears on orders tab and triggers CSV; (3) collapse button on LogPanel shows chevron icon; (4) inter-card gap consistent on dashboard

## Tests
- pytest: no
- svelte-check: yes (gate before commit)
- playwright: yes

## Commit message
refactor(card-header): unify LogPanel into CardHeader — scrollable middle, chevron collapse, download hides on news tab, inter-card spacing normalized

## Done when
1. LogPanel label-branch uses CardHeader — no `lp-card-btns` markup remaining in the live path
2. Collapse button on activity card shows chevron, matches all other cards
3. Download button hidden (not just disabled) when on news tab; appears and fires CSV on orders/agents/system tabs
4. Middle zone (tabs + filters) scrolls horizontally if it overflows, never pushing card buttons off-screen
5. Fullscreen button on activity card opens ActivityLogModal (existing behaviour preserved)
6. All page files use consistent `0.6rem` inter-card gap
7. `svelte-check` clean, Playwright green

## Critical files
- `frontend/src/lib/CardHeader.svelte` — add `hideFullscreen` prop (Phase 1)
- `frontend/src/lib/LogPanel.svelte` — replace label-branch rows 1419–1505 (Phase 2)
- `frontend/src/lib/CardControls.svelte` — read-only reference, no change needed
- `frontend/src/app.css` — global inter-card gap rule (Phase 3)
- Page files listed above — spacing normalization (Phase 3)
