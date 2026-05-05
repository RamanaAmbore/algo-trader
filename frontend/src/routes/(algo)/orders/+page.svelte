<script>
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { clientTimestamp, logTime, visibleInterval } from '$lib/stores';
  import { fetchOrders, cancelOrder, modifyOrder, fetchRecentAgentEvents, fetchAdminLogs } from '$lib/api';
  import LogPanel from '$lib/LogPanel.svelte';
  import CommandBar from '$lib/CommandBar.svelte';
  import OrderDetail from '$lib/OrderDetail.svelte';
  import OrderTicket from '$lib/order/OrderTicket.svelte';
  import { loadInstruments, getInstrument } from '$lib/data/instruments';
  import { loadAccounts } from '$lib/data/accounts';
  import { orderGrammar, setQuoteLoadedCallback, previewSymbol, enrichOrderPairs, buildOrderPayload } from '$lib/command/grammars/orders';
  import { createPerformanceSocket } from '$lib/ws';

  let orders        = $state([]);
  let loading       = $state(true);
  let error         = $state('');
  let success       = $state('');
  let filterStatus  = $state('all');
  let cmdVerb       = $state('');
  let running       = $state(false);
  let logTab        = $state('order');
  let orderLog      = $state([]);
  let agentLog      = $state([]);
  let systemLog     = $state([]);
  let cmdHistory    = $state([]);
  let selectedOrder = $state(/** @type {any|null} */(null));
  // OrderTicket props built when the operator types `buy …` / `sell …`
  // — Phase 2 of the order-entry unification: every order surface
  // routes through the same modal so CHASE + L/M/H + depth auto-fill +
  // per-account picker apply uniformly. Cancel / modify stay direct
  // API calls (no modal needed for those — they're targeted ops).
  let orderTicketProps = $state(/** @type {any|null} */(null));
  let cmdBar;
  let unsub;
  let logTeardown;

  // context for CommandBar — keeps openOrderIds fresh so cancel/modify suggest them
  const cmdContext = $derived({
    openOrders: orders
      .filter(o => o.status === 'OPEN' || o.status === 'TRIGGER PENDING'),
  });

  async function loadOrders() {
    loading = true; error = '';
    try { const d = await fetchOrders(); orders = d.rows || []; }
    catch (e) { error = e.message; }
    finally { loading = false; }
  }

  function addResult(/** @type {string} */ status, /** @type {string} */ message, /** @type {Record<string,string>} */ fields = {}) {
    const t = logTime(new Date());
    cmdHistory = [{ status, message, fields, time: t }, ...cmdHistory].slice(0, 100);
    logTab = 'order';
  }

  async function runParsed(parsed) {
    running = true;
    try {
      if (parsed.verb === 'buy' || parsed.verb === 'sell') {
        // Phase 2 unification — instead of POSTing the parsed
        // payload directly, open OrderTicket pre-filled. The ticket
        // owns submit (PAPER / LIVE), depth ladder, account picker,
        // CHASE + L/M/H. Same surface as /admin/options + the
        // dashboard row-click. Cancel / modify keep their direct
        // path below — they're single-target ops, no modal needed.
        const payload = buildOrderPayload(parsed);
        if (!payload) throw new Error(`couldn't build order payload`);
        const sym  = String(payload.tradingsymbol || '').toUpperCase();
        const inst = getInstrument(sym);
        const lot  = Number(inst?.ls || 1);
        orderTicketProps = {
          symbol:    sym,
          exchange:  payload.exchange || inst?.e || 'NFO',
          side:      payload.transaction_type,
          action:    'open',
          qty:       Number(payload.quantity) || 0,
          lotSize:   lot,
          orderType: payload.order_type || 'LIMIT',
          price:     payload.price > 0 ? payload.price : undefined,
          trigger:   payload.trigger_price > 0 ? payload.trigger_price : undefined,
          product:   payload.product,
          accounts:  [],
          account:   String(payload.account || ''),
          // Orders page has no drafts panel — start on PAPER, allow
          // LIVE escalation.
          defaultMode:    'live',
          availableModes: ['live'],
        };
        addResult('…', `Opening ticket`, {
          verb: parsed.verb.toUpperCase(),
          account: parsed.args.account,
          symbol: payload.tradingsymbol, exchange: payload.exchange,
          qty: String(payload.quantity), type: payload.order_type,
          price: String(payload.price || ''), product: payload.product,
        });
        cmdBar?.clear();
        // Short-circuit — the ticket's onSubmit handler will fire
        // loadOrders() once the operator confirms. Skipping the
        // loadOrders() at the bottom of this function avoids a
        // wasted poll while the modal is still open.
        running = false;
        return;
      } else if (parsed.verb === 'cancel') {
        const id = parsed.args.order_id;
        const ord = orders.find(o => o.order_id === id);
        if (!ord) throw new Error(`order ${id} not found`);
        await cancelOrder(id, ord.account);
        addResult('✓', `Order cancelled`, { id, symbol: ord.tradingsymbol });
      } else if (parsed.verb === 'modify') {
        const id = parsed.args.order_id;
        const ord = orders.find(o => o.order_id === id);
        if (!ord) throw new Error(`order ${id} not found`);
        const p = { account: ord.account };
        if (parsed.kwargs.price != null) p.price = parsed.kwargs.price;
        if (parsed.kwargs.qty != null) p.quantity = parsed.kwargs.qty;
        await modifyOrder(id, p);
        const mods = {};
        if (parsed.kwargs.price != null) mods.price = String(parsed.kwargs.price);
        if (parsed.kwargs.qty != null) mods.qty = String(parsed.kwargs.qty);
        addResult('✓', `Order modified`, { id, ...mods });
      }
      await loadOrders();
      cmdBar?.clear();
    } catch (e) {
      addResult('✗', e.message, {});
    } finally {
      running = false;
    }
  }

  async function loadOrderLog() {
    try { orderLog = await fetchRecentAgentEvents(100); }
    catch (e) { /* ignore */ }
  }
  async function loadAgentLog() {
    try { agentLog = await fetchRecentAgentEvents(100); }
    catch (e) { /* ignore */ }
  }
  async function loadSystemLog() {
    try {
      const d = await fetchAdminLogs(100);
      systemLog = d.lines || [];
    } catch (e) { /* ignore */ }
  }
  function loadCurrentLog() {
    if (logTab === 'order') loadOrderLog();
    else if (logTab === 'agent') loadAgentLog();
    else if (logTab === 'system') loadSystemLog();
  }

  function orderEnrichPairs(pairs, ctx) {
    cmdVerb = (ctx?._verb || '').toUpperCase();
    return enrichOrderPairs(pairs, ctx);
  }
  const statusDataAttr = (/** @type {string} */ s) => {
    const c = s?.toUpperCase();
    if (c === 'COMPLETE') return 'active';
    if (c === 'REJECTED' || c === 'CANCELLED') return 'error';
    if (c === 'OPEN' || c === 'TRIGGER PENDING') return 'running';
    return 'inactive';
  };
  const txnColor = (/** @type {string} */ t) => t === 'BUY' ? 'text-green-400' : 'text-red-400';
  // Industry standard: distinct hues per account, readable on dark bg
  const ACCT_COLORS = ['text-sky-300', 'text-amber-300', 'text-fuchsia-300', 'text-teal-300'];
  const _acctList = /** @type {string[]} */ ([]);
  const acctColor = (/** @type {string} */ a) => {
    let idx = _acctList.indexOf(a);
    if (idx < 0) { _acctList.push(a); idx = _acctList.length - 1; }
    return ACCT_COLORS[idx % ACCT_COLORS.length];
  };

  onMount(() => {
    loadOrders(); loadCurrentLog();
    loadAccounts().catch(() => {});
    loadInstruments().catch(() => {});
    // When an async quote fetch completes, re-render the command bar so the
    // price ladder popup appears without the user having to type another char.
    setQuoteLoadedCallback(() => cmdBar?.refresh());
    unsub = createPerformanceSocket((msg) => {
      if (msg.event === 'order_update') {
        const fields = {
          status: msg.status, verb: msg.transaction_type,
          symbol: msg.tradingsymbol, qty: String(msg.quantity || ''),
          ...(msg.price ? { price: String(msg.price) } : {}),
          ...(msg.account ? { account: msg.account } : {}),
          id: msg.order_id,
        };
        const statusIcon = msg.status === 'COMPLETE' ? '✓' : msg.status === 'REJECTED' ? '✗' : '⟳';
        addResult(statusIcon, `Postback: ${msg.status}${msg.status_message ? ' — ' + msg.status_message : ''}`, fields);
        loadOrders();
      } else if (msg.event === 'performance_updated') {
        loadOrders();
      }
    });
    logTeardown = visibleInterval(loadCurrentLog, 30000);
  });
  onDestroy(() => { unsub?.(); logTeardown?.(); });
</script>

<svelte:head><title>Orders | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col h-[calc(100vh-8rem)]">
<div class="page-header">
  <h1 class="page-title-chip">Orders</h1>
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

{#if error}<div class="mb-1 p-1.5 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>{/if}
{#if success}<div class="mb-1 p-1.5 rounded bg-green-50 text-green-700 text-xs border border-green-200">{success}</div>{/if}

<!-- Order Entry -->
<div class="mt-1 mb-2 relative">
  <CommandBar
    bind:this={cmdBar}
    grammar={orderGrammar}
    context={cmdContext}
    rows={2}
    placeholder={cmdContext.openOrders?.length
      ? "buy | sell | cancel | modify"
      : "buy | sell"}
    onsubmit={runParsed}
    previewFn={previewSymbol}
    enrichPairs={orderEnrichPairs}
    disabled={running}
  />
  <div class="absolute bottom-1 right-2 flex gap-1 z-10">
    <!-- Submit / BUY / SELL — reuses the simulator button palette so
         order entry reads like the rest of the algo console. BUY and
         the generic Submit both use the shared light-green "go" tone
         (sim-btn-primary) — matching Terminal Run and Simulator Start.
         SELL keeps the red sim-btn-danger. -->
    <button onclick={() => cmdBar?.submit()} disabled={running}
      class="sim-btn sim-btn-order
        {cmdVerb === 'SELL' ? 'sim-btn-danger' : 'sim-btn-primary'}
        disabled:opacity-40">
       {cmdVerb === 'BUY' ? 'BUY' : cmdVerb === 'SELL' ? 'SELL' : 'Submit'}
    </button>
    <button onclick={() => { cmdBar?.clear(); cmdVerb = ''; }}
      class="sim-btn sim-btn-order sim-btn-secondary">Clear</button>
  </div>
</div>

<!-- Status Dashboard -->
<div class="grid grid-cols-5 gap-2 mt-1 mb-2">
  <button onclick={() => filterStatus = 'all'}
    class="algo-status-card p-2 text-center {filterStatus === 'all' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="inactive">
    <div class="text-xs font-bold text-[#c8d8f0]">{orders.length}</div>
    <div class="text-[0.55rem] text-[#7e97b8] uppercase">All</div>
  </button>
  <button onclick={() => filterStatus = 'open'}
    class="algo-status-card p-2 text-center {filterStatus === 'open' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="running">
    <div class="text-xs font-bold text-amber-400">{orders.filter(o => o.status === 'OPEN' || o.status === 'TRIGGER PENDING').length}</div>
    <div class="text-[0.55rem] text-[#7e97b8] uppercase">Open</div>
  </button>
  <button onclick={() => filterStatus = 'complete'}
    class="algo-status-card p-2 text-center {filterStatus === 'complete' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="active">
    <div class="text-xs font-bold text-green-400">{orders.filter(o => o.status === 'COMPLETE').length}</div>
    <div class="text-[0.55rem] text-[#7e97b8] uppercase">Filled</div>
  </button>
  <button onclick={() => filterStatus = 'rejected'}
    class="algo-status-card p-2 text-center {filterStatus === 'rejected' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="error">
    <div class="text-xs font-bold text-red-400">{orders.filter(o => o.status === 'REJECTED').length}</div>
    <div class="text-[0.55rem] text-[#7e97b8] uppercase">Rejected</div>
  </button>
  <button onclick={() => filterStatus = 'cancelled'}
    class="algo-status-card p-2 text-center {filterStatus === 'cancelled' ? 'ring-2 ring-[#fbbf24]/40' : ''}" data-status="error">
    <div class="text-xs font-bold text-red-300">{orders.filter(o => o.status === 'CANCELLED').length}</div>
    <div class="text-[0.55rem] text-[#7e97b8] uppercase">Cancelled</div>
  </button>
</div>

<!-- Order Cards -->
{#if loading && !orders.length}
  <div class="text-center text-muted text-xs animate-pulse py-2">Loading orders…</div>
{:else if orders.length}
  <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 mb-1 max-h-[8rem] overflow-y-auto">
    {#each orders.filter(o => filterStatus === 'all' ? true : filterStatus === 'open' ? (o.status === 'OPEN' || o.status === 'TRIGGER PENDING') : o.status === filterStatus.toUpperCase()) as o}
      <button type="button" onclick={() => selectedOrder = (selectedOrder?.order_id === o.order_id ? null : o)}
        class="algo-status-card text-left p-2.5 transition" data-status={statusDataAttr(o.status)}>
        <div class="flex items-center justify-between mb-0.5">
          <span class="font-semibold text-xs"><span class="{txnColor(o.transaction_type)}">{o.transaction_type}</span> <span class="{acctColor(o.account)}">{o.account}</span> <span class="text-[#c8d8f0]">{o.tradingsymbol}</span></span>
          <span class="text-[0.55rem] px-1.5 py-0.5 rounded font-medium uppercase border
            {o.status === 'COMPLETE' ? 'bg-green-500/15 text-green-400 border-green-500/40'
            : o.status === 'REJECTED' ? 'bg-red-500/15 text-red-400 border-red-500/40'
            : 'bg-amber-500/15 text-amber-400 border-amber-500/40'}">{o.status}</span>
        </div>
        <div class="text-[0.55rem] text-[#c8d8f0]/70 flex flex-wrap gap-x-2 uppercase">
          <span>QTY:<b>{o.filled_quantity}/{o.quantity}</b></span>
          <span>ORDER:<b>{o.order_type}</b></span>
          <span>PRICE:<b>{o.average_price || o.price || '—'}</b></span>
          {#if o.trigger_price}<span>TRIGGER:<b>{o.trigger_price}</b></span>{/if}
          <span>PRODUCT:<b>{o.product}</b></span>
          <span>VARIETY:<b>{o.variety}</b></span>
          {#if o.tag}<span>TAG:<b>{o.tag}</b></span>{/if}
          {#if o.status_message}<span>MSG:<b>{o.status_message}</b></span>{/if}
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

<LogPanel
  heightClass="flex-1 min-h-0"
  initialTab={logTab}
  {cmdHistory}
  {orderLog}
  {agentLog}
  {systemLog}
  onTabChange={(id) => { logTab = id; loadCurrentLog(); }}
/>
</div>

{#if orderTicketProps}
  <OrderTicket
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
      // Modify path — payload carries `action: 'modify'` and the
      // modified fields. Refresh the orders list so the operator
      // sees the new price / qty in the row immediately.
      if (payload?.action === 'modify') {
        addResult('✓', `Order modified`, {
          id:      String(payload.orderId || ''),
          price:   String(payload.price ?? ''),
          qty:     String(payload.quantity ?? ''),
          account: payload.account,
        });
        loadOrders();
        return;
      }
      // PAPER + LIVE submissions already hit the backend before
      // onSubmit fires (the ticket awaits placeTicketOrder). Refresh
      // the orders list so the new row appears immediately, and log
      // the result alongside the operator's command echo so the
      // history reads as a single coherent flow.
      if (payload?.mode === 'draft') return;
      addResult('✓', `Order submitted (${(payload.mode || '').toUpperCase()})`, {
        verb:    payload.side,
        symbol:  payload.symbol,
        qty:     String(payload.quantity),
        type:    payload.order_type,
        price:   String(payload.price || ''),
        account: payload.account,
      });
      loadOrders();
    }}
    onClose={() => orderTicketProps = null}
  />
{/if}
