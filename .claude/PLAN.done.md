# Plan: Unified Order Management — Modify/Cancel Pending + Draft Lifecycle

## Context

The order ticket now supports draft mode (session-only payoff planning). ChaseCard shows
active chase loops + drafts. But OPEN/TRIGGER_PENDING broker orders are not in ChaseCard
yet, and the order ticket modal has no "Cancel Order" button for the modify path. This plan
completes the unified in-flight order management model: one card shows all in-flight orders,
one modal handles all mutations.

## What Already Exists (no changes needed)

- `PUT /api/orders/{order_id}` — modify route (`backend/api/routes/orders.py:1560`)
- `DELETE /api/orders/{order_id}?account=&variety=` — cancel route (`backend/api/routes/orders.py:1704`)
- `modifyOrder()` + `cancelOrder()` — frontend API calls (`frontend/src/lib/api.js:703,754`)
- OrderTicket `action='modify'` submit path — calls `buildModifyPayload` + `modifyOrder` (`OrderTicket.svelte:1791`)
- `_buildModifyProps()` on orders page — builds prefill for modify modal (`orders/+page.svelte:219`)
- ChaseCard: chase loops + drafts shown; draft click → `_openDraftTicket` → `initialDraftId` prop
- `initialDraftId` prop in OrderTicket + "Update Draft" / delete-on-cancel draft logic

## Issue Register

| ID | Gap | File | Line |
|---|---|---|---|
| M1 | No "Cancel Order" button in OrderTicket footer for modify mode | OrderTicket.svelte | ~2400 |
| M2 | ChaseCard has no `pendingOrders` prop — OPEN/TRIGGER_PENDING orders absent | ChaseCard.svelte | — |
| M3 | Orders page does not pass pending orders to ChaseCard | orders/+page.svelte | — |
| M4 | `cancelOrder()` result not wired to close modal + reload orders | orders/+page.svelte | — |

---

## Agents

### frontend: Implement M1–M4

**M1 — "Cancel Order" button in OrderTicket footer (action='modify')**

File: `frontend/src/lib/order/OrderTicket.svelte` (~line 2390–2420, footer area).

When `action === 'modify'` AND `orderId` is non-empty, render a secondary red "Cancel Order"
button in the footer (left of the Modify submit button). On click:
- Call `cancelOrder(orderId, _account, _variety)` from `$lib/api.js`
- On success: set `submitOk = 'Order cancelled'`, call `onSubmit({ action: 'cancel', orderId })`, then call `onClose()`
- On error: set `submitErr = e?.message`
- Show spinner during request (same `submitting` flag)

Label the main submit button "Modify Order" when `action='modify'` (currently it shows the
side label; confirm and clarify if needed).

**M2 — ChaseCard: `pendingOrders` prop + render OPEN/TRIGGER_PENDING rows**

File: `frontend/src/lib/order/ChaseCard.svelte`.

Add props:
```js
pendingOrders = /** @type {any[]} */ ([]),
onPendingModify = /** @type {((o: any) => void) | undefined} */ (undefined),
```

Render a section between chase rows and draft rows. For each pending order row:
- Show status chip: "OPEN" (green) or "PENDING" (amber) based on `o.status`
- Show: symbol, qty, price, side chip (BUY/SELL), account
- Click row → `onPendingModify?.(o)`
- NO inline × button (cancel is done through the modal's Cancel Order button)

Update the total count chip: `_chases.length + pendingOrders.length + draftOrders.length`.

**M3 — Orders page: derive `_pendingOrders`, pass to ChaseCard, wire callbacks**

File: `frontend/src/routes/(algo)/orders/+page.svelte`.

1. Derive pending orders (non-chase OPEN/TRIGGER_PENDING):
```js
const _pendingOrders = $derived(
  orders.filter(o =>
    (o.status === 'OPEN' || o.status === 'TRIGGER PENDING' || o.status === 'TRIGGER_PENDING') &&
    !_activeChaseIds.has(String(o.order_id))   // exclude orders already shown as chase loops
  )
);
```
Where `_activeChaseIds` is derived from ChaseCard's chase list (expose via a bindable prop or
by reading ChaseCard's data — use whatever is least invasive, e.g. derive from `chases` API
response that ChaseCard already fetches and exposes via `bind:activeCount`).

If `_activeChaseIds` is not easily accessible, a simpler alternative: filter out orders whose
`order_id` appears in the AlgoOrders chase list. Use a `Set` built from the chase API response
to filter.

2. Pass to ChaseCard:
```svelte
<ChaseCard ... pendingOrders={_pendingOrders}
  onPendingModify={(o) => { orderTicketProps = _buildModifyProps(o); }} />
```

**M4 — Wire cancel result to close modal + reload**

File: `frontend/src/routes/(algo)/orders/+page.svelte`.

The SymbolPanel's `onSubmit` callback already handles `action='modify'` results.
Extend it to handle `action='cancel'`:
```js
function onTicketSubmit(result) {
  if (result.action === 'modify' || result.action === 'cancel') {
    orderTicketProps = null;  // close modal
    loadOrders();             // refresh orders list
  }
}
```
Pass `onSubmit={onTicketSubmit}` to SymbolPanel (or extend the existing submit handler).

---

## Tests

- pytest: no — no backend changes
- svelte-check: yes
- playwright: yes — add to `frontend/e2e/draft-positions.spec.ts` or new file:
  - Pending order row appears in ChaseCard when OPEN orders exist (mock)
  - Clicking pending row opens order modal with action=modify (symbol pre-filled, "Modify Order" label)
  - "Cancel Order" button visible in modal footer for modify mode
  - Draft rows: click opens modal in draft mode; cancel modal = draft removed (existing test, verify still passes)

## Commit message

feat(orders): unified in-flight order management — pending orders in ChaseCard; Cancel Order in modify modal

## Done when

- OPEN/TRIGGER_PENDING non-chase orders appear in ChaseCard between chase and draft rows
- Clicking a pending row opens the order ticket pre-filled with action='modify'
- "Cancel Order" red button visible in ticket footer for action='modify'
- Cancel Order → cancels broker order, closes modal, reloads orders list
- Draft rows: click → ticket in draft mode; modal Cancel → draft deleted from cache (existing, confirm still works)
- svelte-check 0 errors
