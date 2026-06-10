<script>
  // Expandable order detail panel — appears inline between the order
  // cards grid and the log panel when a card is clicked. Shows full
  // details + Modify / Cancel buttons for open orders.
  //
  // Modify routes back to the parent via `onmodify(order)` which
  // opens the shared OrderTicket pre-filled with action='modify'.
  // Cancel uses a window.confirm dialog + direct API call (matches
  // IBKR's "Cancel Order" affordance — single confirm, no full
  // ticket needed for a one-target op).

  import { cancelOrder } from '$lib/api';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import LegLabel from '$lib/LegLabel.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';

  /** @type {{
   *   order:     any|null,
   *   onclose:   () => void,
   *   onchanged: () => void,
   *   onmodify?: (order: any) => void,
   * }} */
  let {
    order = null,
    onclose = () => {},
    onchanged = () => {},
    onmodify  = /** @type {((order:any)=>void)|undefined} */ (undefined),
  } = $props();

  let busy    = $state(false);
  let error   = $state('');
  let success = $state('');

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);

  $effect(() => {
    if (order) { error = ''; success = ''; }
  });

  function fmt(/** @type {any} */ v, /** @type {any} */ dflt = '—') {
    return (v === null || v === undefined || v === '') ? dflt : v;
  }
  const isOpen = $derived(
    order && (order.status === 'OPEN' || order.status === 'TRIGGER PENDING')
  );

  async function doCancel() {
    if (!order) return;
    // Hard-stop confirm — matches IBKR's Cancel Order pattern. No
    // full ticket modal for a one-click op; the operator sees the
    // order id + symbol + qty in the prompt and accepts / declines.
    const ok = await _confirmRef?.ask({
      title: 'Cancel order?',
      message: `#${order.order_id} · ${order.transaction_type} ${order.quantity} ${formatSymbol(order.tradingsymbol)}<br>Account: ${order.account}`,
      danger: true,
      confirmLabel: 'Cancel Order',
    });
    if (!ok) return;
    busy = true; error = ''; success = '';
    try {
      await cancelOrder(order.order_id, order.account);
      success = 'Cancelled';
      onchanged();
    } catch (e) { error = e.message; }
    finally { busy = false; }
  }

  function doModify() {
    if (!order) return;
    if (typeof onmodify === 'function') onmodify(order);
    // Parent owns the ticket — closing this panel is its call. We
    // leave the panel open so the operator can see the source order
    // alongside the modify modal.
  }
</script>

<ConfirmModal bind:this={_confirmRef} />

{#if order}
  <div class="order-detail rounded-lg border border-gray-300 bg-white p-3 mb-3">
    <div class="flex items-start justify-between mb-2">
      <div>
        <div class="text-xs font-bold text-primary"><LegLabel sym={order.tradingsymbol} />
          <span class="ml-2 text-[0.6rem] font-medium {order.transaction_type === 'BUY' ? 'text-green-600' : 'text-red-600'}">{order.transaction_type} {order.quantity}</span>
          <span class="ml-2 text-[0.55rem] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 uppercase">{order.status}</span>
        </div>
        <div class="text-[0.6rem] text-muted font-mono">{order.order_id}</div>
      </div>
      <button type="button" onclick={onclose} class="text-gray-400 hover:text-gray-700 text-sm">×</button>
    </div>

    <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-[0.6rem] text-text/80 mb-2">
      <div><span class="text-muted">Account:</span> {fmt(order.account)}</div>
      <div><span class="text-muted">Exchange:</span> {fmt(order.exchange)}</div>
      <div><span class="text-muted">Type:</span> {fmt(order.order_type)}</div>
      <div><span class="text-muted">Product:</span> {fmt(order.product)}</div>
      <div><span class="text-muted">Price:</span> {fmt(order.price)}</div>
      <div><span class="text-muted">Avg price:</span> {fmt(order.average_price)}</div>
      <div><span class="text-muted">Filled:</span> {fmt(order.filled_quantity, 0)} / {order.quantity}</div>
      <div><span class="text-muted">Pending:</span> {fmt(order.pending_quantity, 0)}</div>
      <div><span class="text-muted">Trigger:</span> {fmt(order.trigger_price)}</div>
      <div><span class="text-muted">Variety:</span> {fmt(order.variety)}</div>
    </div>

    {#if error}<div class="text-[0.6rem] text-red-600 mb-1">{error}</div>{/if}
    {#if success}<div class="text-[0.6rem] text-green-600 mb-1">{success}</div>{/if}

    {#if isOpen}
      <!-- One-click ops — Modify opens the shared OrderTicket
           (action='modify') in the parent; Cancel uses a hard-stop
           confirm dialog + direct API. Same UX shape as the rest
           of the platform's order surfaces post-Phase 3 unification. -->
      <div class="flex gap-2 items-center">
        <button type="button" onclick={doModify}
          class="btn-secondary text-[0.6rem] py-0.5 px-2" disabled={busy}>Modify</button>
        <button type="button" onclick={doCancel}
          class="text-[0.6rem] text-red-600 border border-red-300 rounded py-0.5 px-2 hover:bg-red-50"
          disabled={busy}>{busy ? '…' : 'Cancel'}</button>
      </div>
    {/if}
  </div>
{/if}
