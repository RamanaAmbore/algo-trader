# Plan: LogPanel date filter + card label pill + OrderTicket refresh button

## Context
Three independent frontend fixes:
1. LogPanel order tab shows previous-day terminal orders — filter them out.
2. `ch-title` amber pill background was added in Cycle 2; user wants plain text, no pill.
3. OrderTicket modal header has only the × close button; user wants a refresh button
   before it (matches ChartModal's pattern: circular-arrow icon, same cyan palette).

## Task
1. Filter previous-day COMPLETE/CANCELLED/REJECTED/EXPIRED orders from LogPanel order tab.
2. Remove background/padding/border-radius from `.ch-title` in CardHeader.svelte.
3. Add a refresh button to `OrderTicket.svelte` header before the × close button.

## Agents
- backend: skip
- frontend: All three changes. Files: LogPanel.svelte, CardHeader.svelte, OrderTicket.svelte.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent brief

### Change A — LogPanel previous-day terminal order filter
File: `frontend/src/lib/LogPanel.svelte`

Add a filter helper before `filteredOrderRows`:
```javascript
const _TERMINAL_STATUSES = new Set(['COMPLETE', 'CANCELLED', 'REJECTED', 'EXPIRED']);
function _applyDateFilter(rows) {
  // IST = UTC + 5:30 — timestamps are already in IST format from broker/backend
  const istNow = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
  const today  = istNow.toISOString().slice(0, 10); // "YYYY-MM-DD" in IST
  return rows.filter(o => {
    const ts   = (o.created_at || o.order_timestamp || '').slice(0, 10);
    const term = _TERMINAL_STATUSES.has((o.status || '').toUpperCase());
    // Drop only: previous-day AND terminal status. Keep everything else.
    return !(ts && ts !== today && term);
  });
}
```

Inside `filteredOrderRows = $derived.by(() => { ... })`, insert after `_applyStatusFilter`:
```javascript
rows = _applyStatusFilter(rows, statusFilter);
rows = _applyDateFilter(rows);            // ← new
rows = _applySymbolFilter(rows, symbolFilter);
```

### Change B — Remove amber pill from card header labels
File: `frontend/src/lib/CardHeader.svelte`

In `.ch-title` (around line 165–167), remove these three lines only:
```css
background: rgba(251, 191, 36, 0.10);
padding: 0.1em 0.45em;
border-radius: 3px;
```
Leave all other `.ch-title` rules intact.

### Change C — Refresh button in OrderTicket header
File: `frontend/src/lib/order/OrderTicket.svelte`

**Script changes:**

Add an internal refresh counter after the `refreshKey` prop (around line 209):
```javascript
let _internalRefreshKey = $state(0);
```

In the margin `$effect` (around line 1479), add `_internalRefreshKey` to `_watchers`:
```javascript
const _watchers = [
  _side, _qty, _account, _product, _type, _variety,
  _price, _trigger, symbol, exchange, _resolvedSymbol,
  refreshKey, _internalRefreshKey,   // ← add _internalRefreshKey
];
```

In the funds refresh `$effect` (around line 1923), also fire on `_internalRefreshKey`:
```javascript
$effect(() => {
  if (refreshKey > 0 || _internalRefreshKey > 0) _refetchFunds();
});
```

Pass `_internalRefreshKey` into `OrderDepth` (around line 2250):
```svelte
<OrderDepth
  ...
  refreshKey={refreshKey + _internalRefreshKey}
  ...
/>
```

**Template change** — `.ot-header` (around line 1939–1948):

Before `<button class="ot-close">`, insert:
```svelte
<button type="button" class="ot-refresh-btn"
        title="Refresh"
        aria-label="Refresh order data"
        disabled={submitting}
        onclick={() => { _internalRefreshKey += 1; }}>
  <svg width="13" height="13" viewBox="0 0 16 16"
       fill="none" stroke="currentColor" stroke-width="1.6"
       stroke-linecap="round" stroke-linejoin="round"
       aria-hidden="true">
    <path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.9" />
    <path d="M13.5 3v3h-3" />
  </svg>
</button>
```

**Style change** — add `.ot-refresh-btn` matching `.ot-close`'s size but with cyan palette:
```css
.ot-refresh-btn {
  width: 1.4rem;
  height: 1.4rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--algo-cyan-bg, rgba(34,211,238,0.08));
  border: 1px solid var(--algo-cyan-border, rgba(34,211,238,0.30));
  border-radius: 3px;
  color: var(--c-info, #22d3ee);
  cursor: pointer;
  padding: 0;
  flex-shrink: 0;
  transition: background 0.08s, border-color 0.08s;
}
.ot-refresh-btn:hover:not(:disabled) {
  background: rgba(34,211,238,0.14);
  border-color: rgba(34,211,238,0.65);
}
.ot-refresh-btn:disabled { opacity: 0.45; cursor: default; }
```

After all changes:
```bash
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -5
```

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(order): add refresh btn to ticket header; hide prev-day terminal orders; remove card label pill bg

## Done when
- OrderTicket modal header shows: symbol info | [refresh icon] [×] — refresh triggers
  margin, funds, and depth re-fetch without closing the modal
- Previous-day COMPLETE/CANCELLED/REJECTED/EXPIRED orders absent from LogPanel order tab
- All card header labels show plain amber text, no pill background
- svelte-check: 0 errors
