# Plan: OrderBook polish + LogPanel order tab → event log

## Context
Two related changes based on operator feedback:

1. **OrderBook visual** — OrderBook.svelte's bespoke `.ob-header` needs to use the reusable
   `CardHeader` component (label chip + CardControls collapse). The card list area should match
   LogPanel's order tab exactly: same responsive grid (1→2→3 col), same padding, same scroll
   containment. The /orders page also needs a bounded height on the activity card body so
   `flex: 1 1 0` on the scroll container works (old ActivityLogSurface applied `height:320px`
   internally via `lp-body-card`; without it the list grows unbounded).

2. **LogPanel order tab → event log** — The order tab in LogPanel should behave like the
   system/conn tab: a chronological event log of order lifecycle events (placed, filled, rejected,
   cancelled, etc.) rendered as log-rows with timestamp chip + kind pill + message. Data source:
   `/api/orders/events/recent` endpoint, returning `AlgoOrderEvent` rows (`ts`, `kind`, `message`).

Tag metadata status: `AppMessage.tags` PG_ARRAY + GIN index exists in the backend DB/model —
backend storage implemented. No frontend UI surfaces it yet — not in scope here.

## Agents
- backend: skip
- frontend: implement all four changes below
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(ui): OrderBook CardHeader + LogPanel order tab → event log

## Done when
- OrderBook header uses CardHeader (label chip + collapse); count shown in left snippet
- OrderBook card grid matches LogPanel order tab: responsive 1/2/3-col grid, same padding/scroll
- /orders page activity card has bounded height so scroll works
- LogPanel order tab shows event log rows (ts + kind pill + message), same row format as system tab
- svelte-check 0 errors

---

## Change 1 — OrderBook.svelte (`frontend/src/lib/OrderBook.svelte`)

### 1a. Add CardHeader import
```js
import CardHeader from '$lib/CardHeader.svelte';
```

### 1b. Add `isCollapsed` bindable prop in `$props()` destructure
```js
isCollapsed = $bindable(false),
```

### 1c. Replace `.ob-header` div with CardHeader
Remove `<div class="ob-header">...</div>`. Add:
```svelte
<CardHeader label={title} showSearch={false} bind:isCollapsed>
  {#snippet left()}
    <span class="ob-count">{filteredOrderRows.length} order{filteredOrderRows.length !== 1 ? 's' : ''}</span>
  {/snippet}
</CardHeader>
```

### 1d. Guard scroll body on collapse
```svelte
{#if !isCollapsed}
  <div class="ob-scroll">
    ...existing content...
  </div>
{/if}
```

### 1e. CSS — remove old header rules, align scroll+grid with LogPanel
Remove `.ob-header { ... }` and `.ob-label { ... }` entirely.

Update `.ob-scroll` to match LogPanel's `.lp-order-scroll`:
```css
.ob-scroll {
  overflow-y: auto;
  flex: 1 1 0;
  min-height: 0;
  padding: 0.4rem 0.2rem;
}
```

Add responsive grid (scoped, mirrors LogPanel's `.oc-book-grid` rules):
```css
.ob-scroll :global(.oc-book-grid) {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0.5rem;
}
@media (min-width: 640px) {
  .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (min-width: 1024px) {
  .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
```

Keep `.ob-count` (now lives inside CardHeader left snippet):
```css
.ob-count {
  font-size: 0.65rem;
  color: rgba(255,255,255,0.3);
  font-variant-numeric: tabular-nums;
  margin-left: 0.25rem;
}
```

---

## Change 2 — /orders page (`frontend/src/routes/(algo)/orders/+page.svelte`)

Add bounded height on the activity card body:
```svelte
<div class="card-body oc-act-body"
     style="display:flex; flex-direction:column; height:320px; overflow:hidden;">
```

---

## Change 3 — api.js (`frontend/src/lib/api.js`)

Add `fetchOrderEvents()` alongside existing fetch functions:
```js
/** @param {number} [limit=200] */
export async function fetchOrderEvents(limit = 200) {
  const r = await apiFetch(`/api/orders/events/recent?limit=${limit}&status=all`);
  return Array.isArray(r) ? r : (r?.events ?? []);
}
```

---

## Change 4 — LogPanel.svelte (`frontend/src/lib/LogPanel.svelte`)

### 4a. Import fetchOrderEvents
Add `fetchOrderEvents` to the import from `$lib/api`.

### 4b. Add orderEvents state + load in refresh loop
```js
let orderEvents = $state(/** @type {any[]} */ ([]));
```
In the existing `_refresh()` / polling function (where systemLog and connEvents are loaded),
add a parallel fetch:
```js
fetchOrderEvents(200).then(evts => { orderEvents = evts; }).catch(() => {});
```

### 4c. Kind → CSS class (add helper function)
```js
function _orderEvtCls(kind) {
  switch (kind) {
    case 'placed':          return 'log-row-info';
    case 'fill':            return 'log-row-ok';
    case 'reject':          return 'log-row-error';
    case 'cancel':          return 'log-row-warn';
    case 'preflight_block': return 'log-row-error';
    case 'preflight_ok':    return 'log-row-ok';
    default:                return 'log-row-debug';
  }
}
```

### 4d. Add filtered order events derived value
```js
const filteredOrderEvents = $derived.by(() => {
  let evts = orderEvents;
  if (_internalAccountFilter.length) {
    const want = new Set(_internalAccountFilter);
    evts = evts.filter(e => !e.account || want.has(String(e.account)));
  }
  if (_searchQuery) {
    const q = _searchQuery.toLowerCase();
    evts = evts.filter(e =>
      (e.message || '').toLowerCase().includes(q) ||
      (e.kind || '').toLowerCase().includes(q)
    );
  }
  return evts;
});
```

### 4e. Replace order tab markup with event log rows
Find the `{#if logTab === 'order'}` (or equivalent) block that currently renders the
`<div class="lp-order-scroll">` with OrderCard rows. Replace the inner content with:
```svelte
<div class="lp-order-scroll {heightClass}">
  {#if filteredOrderEvents.length}
    {#each filteredOrderEvents as evt (evt.id)}
      <div class="log-row {_orderEvtCls(evt.kind)}">
        {@html _dualTsHtml(evt.ts)}
        <span class="log-row-tag">{(evt.kind || '').replace(/_/g, ' ').toUpperCase()}</span>
        <span class="log-msg">{evt.message || ''}</span>
      </div>
    {/each}
  {:else}
    <div class="log-debug py-2 text-center">No order events.</div>
  {/if}
</div>
```

This reuses `_dualTsHtml()`, `.log-row`, `.log-row-tag`, `.log-msg` CSS already in LogPanel —
same density and format as the system tab.

---

## Critical files
- `frontend/src/lib/OrderBook.svelte` — CardHeader, isCollapsed, scroll/grid CSS
- `frontend/src/routes/(algo)/orders/+page.svelte` — bounded height on activity card body
- `frontend/src/lib/api.js` — add `fetchOrderEvents()`
- `frontend/src/lib/LogPanel.svelte` — order tab → event log rows
