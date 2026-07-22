# Plan: Modal + Card Header Polish — Button Order, Highlighter, Sheet Modals

## Context

Six targeted issues all touching CardHeader → CardControls → LogPanel → modal sizing:

1. **Button sequence wrong** — LogPanel's right slot renders *before* CardControls, so Close × appears before Search/Download/Collapse. Holdings canonical order is Refresh · Search · Download · Collapse · Fullscreen/DefaultSize.
2. **No label highlighter** — `ch-title` is plain text; no background pill behind the label on any card.
3. **BellIcon after Log label in modal** — LogPanel's `left` snippet renders BellIcon when `context === 'modal'`. User wants it removed.
4. **Modal Refresh hidden + Collapse visible in modal** — Refresh only shows when `isFullscreen || refreshAlwaysVisible`; neither is true in modal context. Collapse always shows but is meaningless inside a fullscreen modal.
5. **Modals don't fill available space** — `canonical-modal-panel` is `min(78vw, 980px) × min(82vh, 760px)` centered. User wants full-width, full-height from bottom of page header to viewport bottom.
6. **ChartModal refresh is a static span, not a button** — `cm-refresh-wrap` is a visual-only `<span>`. Should be a clickable `<button>` consistent with canonical card button chrome.

## Button rule (critical invariant)

| How modal opens | Close control |
|---|---|
| Card **fullscreen button** (in-place portal, e.g. Holdings in MarketPulse) | **DefaultSizeButton** replaces FullscreenButton in CardControls — already correct via existing FullscreenButton/DefaultSizeButton pair |
| **Page-header button** (Activity, Charts) → separate modal component | **× close button** — ActivityLogModal and ChartModal already correct; preserve this |

No change needed to the Holdings/MarketPulse fullscreen pattern — it already uses DefaultSizeButton correctly via CardControls.

---

## Changes

### 1 — `CardHeader.svelte`

**a. Swap right-slot order** — move `{@render right?.()}` to render AFTER CardControls:
```svelte
<div class="ch-right">
  {#if showControls}
    <CardControls ... />
  {/if}
  {@render right?.()}   ← was before CardControls, now after
</div>
```
Result: Close × (right slot) always trails canonical button set. Safe for SymbolPanel (`showControls={false}` → right slot renders alone, order unchanged).

**b. Add `showCollapse` prop** (forwarded to CardControls, default `true`):
```js
showCollapse = true,
```

**c. Add label highlighter to `.ch-title`:**
```css
.ch-title {
  /* add to existing styles */
  background: rgba(251, 191, 36, 0.10);
  padding: 0.1em 0.45em;
  border-radius: 3px;
}
```
Applies to all CardHeader users automatically.

---

### 2 — `CardControls.svelte`

Add `showCollapse = true` prop:
```svelte
{#if showCollapse}
  <CollapseButton bind:isCollapsed {cardId} {label} />
{/if}
```

---

### 3 — `LogPanel.svelte`

**a. Remove BellIcon** — delete the entire `{#snippet left()}` block (only contained the conditional BellIcon; the `ch-title` label already serves as the visual identifier).

**b. Wire context-aware props:**
```svelte
<CardHeader
  title={label}
  {cardId}
  {onRefresh}
  refreshAlwaysVisible={context === 'modal'}
  showCollapse={context !== 'modal'}
  bind:isCollapsed
  bind:refreshLoading
  bind:filter={_searchQuery}
  showSearch={true}
  onDownload={logTab === 'news' ? null : (onDownload ?? _downloadCsv)}
  hideFullscreen={true}
>
```
- `refreshAlwaysVisible={context === 'modal'}` → Refresh shows in modal
- `showCollapse={context !== 'modal'}` → Collapse hidden inside modal

**c. Remove `.lp-label-icon` CSS** (now unused).

---

### 4 — `ChartModal.svelte` — make refresh a real button

Replace the static `<span class="cm-refresh-wrap">` with a proper `<button>`:
```svelte
<button type="button"
  class="cm-refresh-btn"
  title={_loading ? 'Refreshing…' : 'Refresh chart'}
  aria-label="Refresh chart"
  disabled={_loading}
  onclick={() => { /* trigger ChartWorkspace reload */ }}>
  <svg class:cm-refresh-icon-loading={_loading} ...>
    <!-- existing circular-refresh SVG path -->
  </svg>
</button>
```
Style `.cm-refresh-btn` to match canonical card button chrome (1.4rem × 1.4rem, cyan-400 border, 3px radius). This makes ChartModal header pattern: **Refresh · Close ×** — consistent with page-header modal rule.

Check how `_loading` refresh is triggered (likely via ChartWorkspace `bind:loading` — add a callback to force reload, or toggle a prop that ChartWorkspace watches).

---

### 5 — `app.css` — sheet modal sizing

**a. Define `--modal-sheet-top` on `.algo-viewport`:**
```css
.algo-viewport {
  --modal-sheet-top: calc(3rem + 1.8rem);  /* navbar (3rem) + page-header strip (1.8rem) */
}
```

**b. Rewrite `canonical-modal-overlay` to sheet layout:**
```css
.canonical-modal-overlay {
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: var(--z-command);
  display: flex;
  align-items: flex-start;          /* was: center */
  justify-content: flex-start;      /* was: center */
  padding: 0;                       /* was: 1rem */
  padding-top: var(--modal-sheet-top, calc(3rem + 1.8rem));
  box-sizing: border-box;
}
```

**c. Rewrite `canonical-modal-panel` to fill remaining space:**
```css
.canonical-modal-panel {
  pointer-events: auto;
  background: var(--card-bg-gradient);
  border: 1px solid rgba(251, 191, 36, 0.40);
  border-top: none;                 /* flush against page header bottom — no gap */
  border-radius: 0;                 /* was: 6px — full-width flush panel */
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.55), 0 0 0 1px var(--algo-amber-bg-soft);
  width: 100%;                      /* was: min(78vw, 980px) */
  height: calc(100dvh - var(--modal-sheet-top, calc(3rem + 1.8rem)));  /* was: min(82vh, 760px) */
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
```

**d. Remove `@media (max-width: 760px)` canonical-modal-panel override** (superseded by 100% width/height).

---

## Agents

- frontend: Implement all 5 changes above. Read each file before editing.
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
fix(card-header): sheet modals, button order, label highlight, bell icon removed, collapse+refresh in modal context, ChartModal refresh button

## Done when
1. All card labels have amber background pill (highlighter)
2. Close × is last in modal button row (Refresh · Search · Download · Close ×)
3. LogPanel modal: Refresh visible, Collapse hidden, BellIcon gone
4. LogPanel non-modal: Search · Download · Collapse · Fullscreen-open (unchanged)
5. ChartModal refresh is a clickable button with canonical chrome; header: Refresh · Close ×
6. All `canonical-modal-panel` modals fill full width + full height below page header
7. Holdings card fullscreen still uses DefaultSizeButton (no change needed)
8. `svelte-check` 0 errors

## Critical files
- `frontend/src/lib/CardHeader.svelte` — right-slot order, showCollapse prop, ch-title highlight
- `frontend/src/lib/CardControls.svelte` — showCollapse prop
- `frontend/src/lib/LogPanel.svelte` — remove BellIcon snippet, refreshAlwaysVisible+showCollapse
- `frontend/src/lib/ChartModal.svelte` — cm-refresh-wrap → clickable button
- `frontend/src/app.css` — --modal-sheet-top, canonical-modal-overlay sheet, canonical-modal-panel full-size
- `frontend/src/routes/(algo)/+layout.svelte` — --modal-sheet-top CSS var definition (if not done in app.css .algo-viewport)
