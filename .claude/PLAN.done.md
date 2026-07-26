# Plan: Replace ActivityLogSurface with OrderBook in modal + /orders page

## Task
The order modal (SymbolPanel bottom panel) and /orders page currently show a full LogPanel
(ActivityLogSurface with multiple tabs). Replace both with the standalone OrderBook component,
which shows only order cards with cancel/modify/reconcile actions and no unrelated tabs.

Two surfaces:
1. SymbolPanel `.oes-bottom-panel` — replace ActivityLogSurface with OrderBook, statusFilter="open"
2. /orders page Activity card — replace ActivityLogSurface with OrderBook, statusFilter={_statusFilter}

LogPanel's order tab in all other mounts (dashboard, console, ActivityLogModal, etc.) is unchanged.

## Agents
- backend: skip
- frontend: implement all three file changes (OrderBook.svelte prop addition, SymbolPanel.svelte swap, /orders page swap) as described in the critical-files section below
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(ui): replace LogPanel with OrderBook in order modal + /orders page

## Done when
- SymbolPanel bottom panel renders OrderBook (open orders) — no LogPanel tabs visible
- /orders page Activity card renders OrderBook — counter cards (All/Open/Filled/etc.) still filter it via statusFilter prop
- LogPanel / ActivityLogSurface in all other mounts unchanged
- svelte-check 0 errors

---

## Critical files

### Change 1 — OrderBook.svelte (`frontend/src/lib/OrderBook.svelte`)

Add `onSymbolClick` as an optional prop. When provided, call it on symbol click instead of
spawning a nested SymbolPanel (prevents circular rendering when embedded inside SymbolPanel).

**Current props block (lines 30–36):**
```js
let {
  orderId       = null,
  accountFilter = /** @type {string[]} */ ([]),
  title         = 'Order Book',
  pollMs        = 3000,
  statusFilter  = /** @type {'all'|'open'|'complete'|'rejected'|'cancelled'} */ ('all'),
} = $props();
```

**New props block:**
```js
let {
  orderId       = null,
  accountFilter = /** @type {string[]} */ ([]),
  title         = 'Order Book',
  pollMs        = 3000,
  statusFilter  = /** @type {'all'|'open'|'complete'|'rejected'|'cancelled'} */ ('all'),
  onSymbolClick = /** @type {((ord: any) => void) | null} */ (null),
} = $props();
```

**Current OrderCard onSymbolClick (line 232–233):**
```svelte
onSymbolClick={(ord) => { _symPanelSym = ord.tradingsymbol || ord.symbol || ''; _symPanelExch = ord.exchange || ''; }}
```

**New OrderCard onSymbolClick:**
```svelte
onSymbolClick={(ord) => {
  if (onSymbolClick) { onSymbolClick(ord); return; }
  _symPanelSym = ord.tradingsymbol || ord.symbol || '';
  _symPanelExch = ord.exchange || '';
}}
```

**Guard the internal SymbolPanel spawner (lines 285–292) — add `&& !onSymbolClick` guard:**
```svelte
{#if _symPanelSym && !onSymbolClick}
  <SymbolPanel ... />
{/if}
```

### Change 2 — SymbolPanel.svelte (`frontend/src/lib/SymbolPanel.svelte`)

**Import swap (line 47):**
Remove: `import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';`
Add:    `import OrderBook from '$lib/OrderBook.svelte';`

**Bottom panel swap (lines 2974–2981):**
Replace:
```svelte
<ActivityLogSurface
  context="card"
  heightClass="flex-1 min-h-0"
  label="Log"
  defaultTab="order"
  statusFilter="open"
  hideInlineAccountFilter={false}
/>
```
With:
```svelte
<OrderBook
  statusFilter="open"
  onSymbolClick={() => {}}
/>
```

### Change 3 — /orders page (`frontend/src/routes/(algo)/orders/+page.svelte`)

**Import swap:**
Remove: `import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';`
Add:    `import OrderBook from '$lib/OrderBook.svelte';`

**Drop unused variables (only used by ActivityLogSurface — lines 172–174):**
Remove:
```js
let _actAvailableAccounts = $state([]);
let _actLevelFilter = $state('all');
```
Keep `_actAccountFilter` — passed to OrderBook.

**Activity section body swap (lines 549–564):**
Replace the `<ActivityLogSurface ...>` block inside `.card-body.oc-act-body` with:
```svelte
<OrderBook
  statusFilter={_statusFilter}
  accountFilter={_actAccountFilter}
/>
```

Keep the surrounding `<section class="bucket-card bucket-card-activity oc-fill" ...>` wrapper
and `use:listenModifyOrder` directive unchanged — OrderBook dispatches the same `lp:modify-order`
CustomEvent so modify-order handling still works.
