<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { nowStamp, logTimeIst } from '$lib/stores';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import AgentNotifications from '$lib/AgentNotifications.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import { fetchOrders } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';
  import OrderDetail from '$lib/OrderDetail.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import { loadInstruments } from '$lib/data/instruments';
  import { priceFmt, qtyFmt } from '$lib/format';
  import { loadAccounts } from '$lib/data/accounts';
  import { createPerformanceSocket } from '$lib/ws';

  let orders        = $state([]);
  let loading       = $state(true);
  let error         = $state('');
  let filterStatus  = $state('all');
  let selectedOrder = $state(/** @type {any|null} */(null));
  // OrderTicket props for the Modify path — opening an order row's
  // "Modify" pre-fills a SymbolPanel modal at the page-bottom mount.
  // The top-of-page inline SymbolPanel handles open/place flows; this
  // separate modal handles single-target modify so the row context
  // is preserved.
  let orderTicketProps = $state(/** @type {any|null} */(null));
  let unsub;
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);

  async function loadOrders() {
    loading = true; error = '';
    try { const d = await fetchOrders(); orders = d.rows || []; }
    catch (e) { error = e.message; }
    finally { loading = false; }
  }
  const statusDataAttr = (/** @type {string} */ s) => {
    const c = s?.toUpperCase();
    if (c === 'COMPLETE') return 'active';
    if (c === 'REJECTED' || c === 'CANCELLED') return 'error';
    if (c === 'OPEN' || c === 'TRIGGER PENDING') return 'running';
    return 'inactive';
  };
  const txnColor = (/** @type {string} */ t) => t === 'BUY' ? 'color: var(--btn-buy)' : 'color: var(--btn-sell)';
  // Industry standard: distinct hues per account, readable on dark bg
  const ACCT_COLORS = ['text-sky-300', 'text-amber-300', 'text-fuchsia-300', 'text-teal-300'];
  const _acctList = /** @type {string[]} */ ([]);
  const acctColor = (/** @type {string} */ a) => {
    let idx = _acctList.indexOf(a);
    if (idx < 0) { _acctList.push(a); idx = _acctList.length - 1; }
    return ACCT_COLORS[idx % ACCT_COLORS.length];
  };

  onMount(() => {
    loadOrders();
    loadAccounts().catch(() => {});
    loadInstruments().catch(() => {});
    unsub = createPerformanceSocket((msg) => {
      // Either an order postback or a positions/holdings refresh — both
      // mean "re-fetch the order book." The card grid auto-updates from
      // the new `orders` state.
      if (msg.event === 'order_update' || msg.event === 'performance_updated') {
        loadOrders();
      }
    });
  });
  onDestroy(() => { unsub?.(); });
</script>

<svelte:head><title>Orders | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col">
<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Orders</h1>
    <InfoHint popup text="Live order book across every loaded broker account. Click a row for the full status / fill timeline; Cancel and Modify hit the broker directly. Status pills: OPEN (in book), TRIGGER_PENDING (SL waiting), COMPLETE (filled), REJECTED (broker / margin failure)." />
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <RefreshButton onClick={loadOrders} loading={loading} label="orders" />
  <OrderNotifications /><AgentNotifications />
</div>

{#if error}<div class="mb-1 p-1.5 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>{/if}

<!-- Status Dashboard — moved to the top of the page under the strip
     so the operator sees the order-state counters first, before
     anything else. Click a card to filter the order grid below. -->
<div class="grid grid-cols-5 gap-2 mt-1 mb-2">
  <button onclick={() => filterStatus = 'all'}
    class="algo-status-card p-2 text-center {filterStatus === 'all' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="inactive">
    <div class="text-xs font-bold text-[#c8d8f0]">{orders.length}</div>
    <div class="text-[0.62rem] text-[#7e97b8] uppercase">All</div>
  </button>
  <button onclick={() => filterStatus = 'open'}
    class="algo-status-card p-2 text-center {filterStatus === 'open' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="running">
    <div class="text-xs font-bold text-amber-400">{orders.filter(o => o.status === 'OPEN' || o.status === 'TRIGGER PENDING').length}</div>
    <div class="text-[0.62rem] text-[#7e97b8] uppercase">Open</div>
  </button>
  <button onclick={() => filterStatus = 'complete'}
    class="algo-status-card p-2 text-center {filterStatus === 'complete' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="active">
    <div class="text-xs font-bold text-green-400">{orders.filter(o => o.status === 'COMPLETE').length}</div>
    <div class="text-[0.62rem] text-[#7e97b8] uppercase">Filled</div>
  </button>
  <button onclick={() => filterStatus = 'rejected'}
    class="algo-status-card p-2 text-center {filterStatus === 'rejected' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="error">
    <div class="text-xs font-bold text-red-400">{orders.filter(o => o.status === 'REJECTED').length}</div>
    <div class="text-[0.62rem] text-[#7e97b8] uppercase">Rejected</div>
  </button>
  <button onclick={() => filterStatus = 'cancelled'}
    class="algo-status-card p-2 text-center {filterStatus === 'cancelled' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="error">
    <div class="text-xs font-bold text-orange-400">{orders.filter(o => o.status === 'CANCELLED').length}</div>
    <div class="text-[0.62rem] text-[#7e97b8] uppercase">Cancelled</div>
  </button>
</div>

<!-- 3-tab order-entry shell (Command Line · Order Ticket · Chain).
     Same inline SymbolPanel `/console` uses, with Command Line as
     the default tab so the keyboard-first workflow stays unchanged.
     Operators who prefer the form-based ticket or the option-chain
     basket can flip tabs without leaving the page. -->
<div class="oes-inline-wrap mt-1 mb-2">
  <SymbolPanel
    inline
    defaultTab="command"
    symbol=""
    action="open"
    side="BUY"
    onSubmit={(payload) => {
      // Drafts are page-local — no broker write. PAPER / LIVE submits
      // hit the backend; the order grid below picks them up via the
      // WebSocket order_update postback (or this defensive refresh).
      if (payload?.mode === 'draft') return;
      loadOrders();
    }}
    onClose={() => { /* inline mode — no close affordance */ }} />
</div>
{#if isDemo}
  <div class="mb-2 text-[0.62rem] text-[#7e97b8] font-mono">
    Demo: read-only — sign in to place orders
  </div>
{/if}

<!-- Order Cards -->
{#if loading && !orders.length}
  <div class="text-center text-muted text-xs animate-pulse py-2">Loading orders…</div>
{:else if orders.length}
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 mb-1 max-h-[min(30vh,16rem)] overflow-y-auto">
    {#each orders.filter(o => filterStatus === 'all' ? true : filterStatus === 'open' ? (o.status === 'OPEN' || o.status === 'TRIGGER PENDING') : o.status === filterStatus.toUpperCase()) as o}
      <button type="button" onclick={() => selectedOrder = (selectedOrder?.order_id === o.order_id ? null : o)}
        class="algo-status-card text-left p-2.5 transition" data-status={statusDataAttr(o.status)}>
        <div class="flex items-center justify-between mb-0.5">
          <span class="font-semibold text-xs"><span style="{txnColor(o.transaction_type)}">{o.transaction_type}</span> <span class="{acctColor(o.account)}">{o.account}</span> <span class="text-[#c8d8f0]">{o.tradingsymbol}</span></span>
          <span class="text-[0.55rem] px-1.5 py-0.5 rounded font-medium uppercase border
            {o.status === 'COMPLETE' ? 'bg-green-500/15 text-green-400 border-green-500/40'
            : o.status === 'REJECTED' ? 'bg-red-500/15 text-red-400 border-red-500/40'
            : 'bg-amber-500/15 text-amber-400 border-amber-500/40'}">{o.status}</span>
        </div>
        <!-- Order chip row uses the same .log-chip / .log-chip-key
             styles as LogPanel's order log + agent log so the operator
             reads a single chip family across every surface
             (operator feedback: "keep them in sync"). -->
        <div class="flex flex-wrap items-center gap-y-1">
          <span class="log-chip"><span class="log-chip-key">qty:</span>{qtyFmt(o.filled_quantity)}/{qtyFmt(o.quantity)}</span>
          <span class="log-chip"><span class="log-chip-key">type:</span>{o.order_type}</span>
          <span class="log-chip"><span class="log-chip-key">price:</span>{o.average_price != null ? priceFmt(o.average_price) : o.price != null ? priceFmt(o.price) : '—'}</span>
          {#if o.trigger_price}<span class="log-chip"><span class="log-chip-key">trigger:</span>{priceFmt(o.trigger_price)}</span>{/if}
          {#if o.validity}<span class="log-chip"><span class="log-chip-key">validity:</span>{o.validity}</span>{/if}
          <span class="log-chip"><span class="log-chip-key">product:</span>{o.product}</span>
          <span class="log-chip"><span class="log-chip-key">variety:</span>{o.variety}</span>
          {#if o.order_timestamp}<span class="log-chip"><span class="log-chip-key">time:</span>{logTimeIst(o.order_timestamp)}</span>{/if}
          {#if o.tag}<span class="log-chip"><span class="log-chip-key">tag:</span>{o.tag}</span>{/if}
          {#if o.status_message}<span class="log-chip"><span class="log-chip-key">note:</span>{o.status_message}</span>{/if}
        </div>
      </button>
    {/each}
  </div>
{:else}
  <div class="text-center text-muted text-xs py-1 mb-1">No orders today.</div>
{/if}

<OrderDetail order={selectedOrder}
  onclose={() => selectedOrder = null}
  onchanged={async () => { await loadOrders(); if (selectedOrder) selectedOrder = orders.find(o => o.order_id === selectedOrder.order_id) || null; }}
  onmodify={(ord) => {
    // Phase 3: Modify routes through the shared OrderTicket
    // (action='modify'). Pre-fill from the existing order's
    // fields. Symbol + side are locked inside the ticket; price /
    // qty / type / trigger remain editable. Submit hits PUT
    // /api/orders/{id} via modifyOrder().
    if (!ord) return;
    orderTicketProps = {
      symbol:    String(ord.tradingsymbol || '').toUpperCase(),
      exchange:  ord.exchange || 'NFO',
      side:      ord.transaction_type,
      action:    'modify',
      orderId:   String(ord.order_id || ''),
      qty:       Number(ord.quantity) || 0,
      lotSize:   1,
      orderType: ord.order_type || 'LIMIT',
      price:     ord.price > 0 ? ord.price : undefined,
      trigger:   ord.trigger_price > 0 ? ord.trigger_price : undefined,
      product:   ord.product,
      account:   String(ord.account || ''),
      accounts:  [],
      // Modify path doesn't touch /api/orders/ticket — mode pills
      // are hidden by the ticket when action='modify' anyway, so
      // the values here are inert. Pass paper-only to be tidy.
      defaultMode:    'paper',
      availableModes: ['paper'],
    };
  }}
/>

</div>

{#if orderTicketProps}
  <SymbolPanel
    defaultTab='ticket'
    symbol={orderTicketProps.symbol}
    exchange={orderTicketProps.exchange}
    side={orderTicketProps.side}
    action={orderTicketProps.action}
    orderId={orderTicketProps.orderId}
    qty={orderTicketProps.qty}
    lotSize={orderTicketProps.lotSize}
    orderType={orderTicketProps.orderType}
    price={orderTicketProps.price}
    trigger={orderTicketProps.trigger}
    product={orderTicketProps.product}
    accounts={orderTicketProps.accounts}
    account={orderTicketProps.account}
    defaultMode={orderTicketProps.defaultMode}
    availableModes={orderTicketProps.availableModes}
    currentQty={orderTicketProps.currentQty ?? 0}
    onSubmit={(payload) => {
      // Drafts are page-local — no broker write, no refresh needed.
      // Modify and PAPER/LIVE submits hit the backend before this
      // callback fires; loadOrders pulls the new state into the
      // order grid.
      if (payload?.mode === 'draft') return;
      loadOrders();
    }}
    onClose={() => orderTicketProps = null}
  />
{/if}

<style>
  .order-card-num { font-variant-numeric: tabular-nums; }
</style>
