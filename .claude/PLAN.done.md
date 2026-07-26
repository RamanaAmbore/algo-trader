# Plan: Modal backdrop dim + Chart tab in order modal

## Context
Two operator-reported UX gaps after the AppMessage/OrderBook deploy:
1. **Modal visibility**: navbar links work while a canonical modal is open (pointer-events:none on overlay), but the page behind looks identical whether a modal is open or not — no visual cue, so accidentally following a nav link loads a new page hidden under the modal.
2. **Chart in order modal**: order modal (SymbolPanel) has a log panel at the bottom and a chart-icon button that opens ChartModal as a *separate* overlay. Operator wants chart and log together inside the same modal, not two separate modals.

## Agents
- frontend: implement Part 1 (modal backdrop dim in app.css + ChartModal/SymbolPanel Esc handling) + Part 2 (Chart tab in SymbolPanel + ChartWorkspace inline)
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
feat(ui): modal backdrop dim + Chart tab in order modal

## Done when
- All canonical modals (SymbolPanel, ChartModal, ActivityLogModal) show a dim overlay above the sheet panel — navbar area visibly darkened when any canonical modal is open
- SymbolPanel tab strip has a 3rd "Chart" tab (Ticket / Chain / Chart); selecting it renders ChartWorkspace inline for the current symbol at full height of the tab body
- Bottom log panel (ActivityLogSurface) remains visible below the Chart tab body
- Chart-icon button in SymbolPanel header still opens ChartModal (unchanged)
- `ORDER_TABS` and `ORDER_TAB_IDS` updated to include 'chart'
- svelte-check 0 errors
- No change to orders page (log already at bottom) or LogPanel

---

## Part 1 — Modal backdrop dim

### File: `frontend/src/app.css`

`.canonical-modal-overlay` already covers `position: fixed; inset: 0` with `pointer-events: none`.
Add a semi-opaque background fill to the padding area above the sheet panel:

```css
.canonical-modal-overlay {
  /* existing rules unchanged */
  background: rgba(8, 12, 20, 0.42);   /* ← ADD: dims navbar/strip above sheet */
}
```

Keep `pointer-events: none` — intentional (page stays interactive, matches existing comment).
The amber-border panel sits on top of the overlay so the panel itself is unaffected.

**No changes needed to individual modal files** — the overlay is shared across SymbolPanel,
ChartModal, and ActivityLogModal automatically.

---

## Part 2 — Chart tab inside SymbolPanel

### 2a. `frontend/src/lib/order/tabs.js`

Add 'chart' as a third tab:
```js
export const ORDER_TABS = ([
  { id: 'ticket', label: 'Ticket' },
  { id: 'chain',  label: 'Chain'  },
  { id: 'chart',  label: 'Chart'  },
]);
export const ORDER_TAB_IDS = ORDER_TABS.map(t => t.id);
```

Type annotation update: `'chain' | 'ticket' | 'chart'`

### 2b. `frontend/src/lib/SymbolPanel.svelte`

**Imports**: add `import ChartWorkspace from '$lib/ChartWorkspace.svelte'`

**`activeTab` type**: update from `'ticket' | 'chain'` to `'ticket' | 'chain' | 'chart'`

**`_resolveInitialTab()`**: Chart tab should never be the initial default; fallback to 'ticket' if somehow passed.

**`basketMode` derived**: keep as `_activeTab === 'chain'` (chart tab is not basket mode)

**Tab body markup**: add a `{#if _activeTab === 'chart'}` branch alongside the ticket and chain branches:
```svelte
{#if _activeTab === 'chart'}
  <div class="oes-chart-tab-body">
    {#if _localSymbol}
      <ChartWorkspace
        symbol={_localSymbol}
        exchange={_pickedExchange || exchange}
        mode="live" />
    {:else}
      <p class="oes-chart-placeholder">Select a symbol to view chart</p>
    {/if}
  </div>
{/if}
```

**CSS** (scoped to SymbolPanel):
```css
.oes-chart-tab-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.oes-chart-placeholder {
  margin: auto;
  color: var(--algo-slate-dim);
  font-size: 0.8rem;
}
```

**Suppress basket footer** when chart tab active:
The action footer (BUY/SELL buttons + basket controls) is gated by `_activeTab === 'ticket'` conditions already in several places — verify the chart tab doesn't show them. Add `&& _activeTab !== 'chart'` guards where needed (same pattern as chain-tab suppression).

**Important**: The `.oes-bottom-panel` (ActivityLogSurface) sits OUTSIDE and AFTER the tab body; it remains visible regardless of which tab is active — no change needed.

### 2c. ChartWorkspace props

ChartModal passes: `symbol`, `exchange`, `mode="live"` to `ChartWorkspace`. Use the same props from SymbolPanel's `_localSymbol` + `_pickedExchange || exchange`. Do NOT replicate the full ChartModal header chrome — the SymbolPanel header already provides navigation context.

---

## Critical files
- `frontend/src/app.css` — `.canonical-modal-overlay` (line ~359)
- `frontend/src/lib/order/tabs.js` — ORDER_TABS and ORDER_TAB_IDS
- `frontend/src/lib/SymbolPanel.svelte` — tab body block (~line 2164), activeTab type, ChartWorkspace import, CSS
- `frontend/src/lib/ChartWorkspace.svelte` — read-only reference for props API
