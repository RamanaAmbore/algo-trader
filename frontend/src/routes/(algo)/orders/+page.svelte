<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { nowStamp, logTimeIst, formatDualTz } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import { fetchOrders, cancelOrder, reconcileSingleOrder } from '$lib/api';
  import OrderDetail from '$lib/OrderDetail.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import ChaseCard from '$lib/order/ChaseCard.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import { loadInstruments } from '$lib/data/instruments';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import {
    loadAccounts,
    resolveSymbol, resolveAccount,
    setRecentSymbol, setRecentAccount,
  } from '$lib/data/accounts';
  // executionMode is used to gate the Order Activity card's bespoke
  // book tab (same as LogPanel's gateByMode behaviour).
  import { executionMode } from '$lib/stores';
  import { createPerformanceSocket } from '$lib/ws';
  import ChartModal from '$lib/ChartModal.svelte';
  import OrderCard from '$lib/order/OrderCard.svelte';

  // Activity-card tab definitions — matches the ActivityLogModal /
  // LogPanel tab ordering (Orders first, Agents second) and uses
  // the same default tab styling (no per-tab color override) so the
  // two surfaces read identically. Operator: "order page activity
  // tabs and order modal activity tabs are not in sync. they should
  // be like order modal".
  const ACT_TABS = /** @type {const} */ ([
    { id: 'book', label: 'Orders' },
    { id: 'log',  label: 'Agents' },
  ]);

  let orders        = $state([]);
  let loading       = $state(true);
  let error         = $state('');
  // Default the status filter to ALL — matches every other LogPanel
  // mount (Activity modal, Order modal bottom panel, /console, /automation)
  // where the Orders tab opens un-filtered ("All" chip clicked). Earlier
  // /orders defaulted to OPEN, which made it the only surface where
  // landing on the Orders tab hid every non-open row.
  let filterStatus  = $state('all');
  // Account + exchange filters on the Order Book card. AccountMultiSelect
  // is the same component pulse + dashboard use, so the filter UX is
  // identical across surfaces.
  let _accountFilter  = $state(/** @type {string[]} */ ([]));
  let _exchangeFilter = $state('all');
  let selectedOrder = $state(/** @type {any|null} */(null));

  // Page-level Symbol picker for the Order Entry card. Seeded from
  // the recent-symbol store → settings default → empty. Operator:
  // "let orders page ... use default symbol if there is no recent
  // symbol is used in charts or orders. if any symbol is used in
  // charts or orders either in model, or page, the symbol should
  // be defaulted to that." A late settings fetch can land after
  // mount; we patch _entrySymbol once the resolved value differs.
  let _entrySymbol   = $state(resolveSymbol());
  // Track the exchange alongside the symbol so commodity options
  // (MCX) and currency contracts (CDS) don't race with the
  // instruments-cache lookup inside OrderTicket / OrderDepth.
  // Operator: "chain and order depth is not getting updated in
  // orders page for commodity options" — the exchange that
  // SymbolSearchInput's onPick meta carries is now persisted so
  // depth + chain see MCX from the first poll.
  let _entryExchange = $state('');

  // Account seeded the same way — recent → settings default → first
  // loaded account in the post-fetch loadAccounts effect below.
  let _entryAccount = $state(resolveAccount());

  // Persist the operator's symbol pick so re-opening the page (or
  // jumping to /charts) reads the same recent symbol.
  $effect(() => { if (_entrySymbol) setRecentSymbol(_entrySymbol); });
  $effect(() => { if (_entryAccount) setRecentAccount(_entryAccount); });
  // Default to 'chain' — basket-building option chain is the most-used
  // surface per operator. Ticket / Command are one click away.
  let _entryActiveTab = $state(/** @type {'chain'|'ticket'} */ ('chain'));
  let _entryAccounts  = $state(/** @type {string[]} */ ([]));

  // Counter-prop dispatch — SymbolPanel's common-actions footer
  // increments these to fire submit/basket on the active tab.
  // OrderTicket inside SymbolPanel reacts via $effect. The page no
  // longer maintains its own mode / side state — SymbolPanel's
  // shared toolbar (_sharedMode, _modalSide) drives both.
  let _triggerSubmit = $state(0);
  let _triggerBasket = $state(0);

  // Per-card collapse + fullscreen state. No persistence (no cardId
  // on CollapseButton) so every page load opens both cards expanded
  // — matches the dashboard pattern.
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _loadOrdersTimer = null;
  function _debouncedLoadOrders() {
    if (_loadOrdersTimer) clearTimeout(_loadOrdersTimer);
    _loadOrdersTimer = setTimeout(loadOrders, 250);
  }

  let _colEntry    = $state(false);
  let _fsEntry     = $state(false);
  let _colActivity = $state(false);
  let _fsActivity  = $state(false);

  // Activity-card tab state. Order Book (card grid) is the default —
  // matches the LogPanel Orders tab format shown in every other
  // surface (Activity modal, Order modal bottom panel, /console,
  // /automation). Agent Log (UnifiedLog of recent events) is one click
  // away. Earlier 'log' was the default but that left the operator
  // landing on a different visual format than every other Orders
  // surface uses.
  let _activityTab = $state(/** @type {'log'|'book'} */ ('book'));

  // OrderTicket props — opens a SymbolPanel modal pre-filled from a
  // row click (Modify / Repeat path). The top-of-page inline shell
  // handles fresh placement; this separate modal handles single-target
  // modify / repeat so row context is preserved without losing the
  // operator's spot on the order book.
  let orderTicketProps = $state(/** @type {any|null} */(null));
  /** @type {{ symbol: string, exchange: string, x: number, y: number } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | 'log' | null} */ let _ctxAction = $state(null);
  /** @type {string} */ let _ctxSym  = $state('');
  /** @type {string} */ let _ctxExch = $state('');
  let unsub;
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);

  async function loadOrders() {
    loading = true; error = '';
    try { const d = await fetchOrders(); orders = d.rows || []; }
    catch (e) { error = e.message; }
    finally { loading = false; }
  }
  // Status check used by per-row action gating (Modify / Cancel buttons).
  const isOpenStatus = (/** @type {string} */ s) =>
    s === 'OPEN' || s === 'TRIGGER PENDING';

  // Available account / exchange values for the filter chips.
  const _availableAccounts = $derived([...new Set(orders.map(o => o.account).filter(Boolean))]);
  const _availableExchanges = $derived([...new Set(orders.map(o => o.exchange).filter(Boolean))]);

  // Single source of truth for which orders the Book card shows.
  // Combines status + account + exchange + executionMode filters.
  // The mode gate mirrors LogPanel's gateByMode behaviour so the
  // /orders page and the Activity modal show the same rows.
  const _filteredOrders = $derived.by(() => {
    return orders.filter(o => {
      const s = o.status;
      if (filterStatus === 'open' && !isOpenStatus(s)) return false;
      if (filterStatus !== 'all' && filterStatus !== 'open' && s !== filterStatus.toUpperCase()) return false;
      if (_accountFilter.length && !_accountFilter.includes(o.account)) return false;
      if (_exchangeFilter !== 'all' && o.exchange !== _exchangeFilter) return false;
      // Mode gate: broker orders (no .mode field) count as 'live'.
      const oMode = o.mode || 'live';
      if ($executionMode && oMode !== $executionMode) return false;
      return true;
    });
  });

  // Shared helper — builds the orderTicketProps shape for a modify action.
  function _buildModifyProps(/** @type {any} */ o) {
    return {
      symbol:    String(o.tradingsymbol || '').toUpperCase(),
      exchange:  o.exchange || 'NFO',
      side:      o.transaction_type,
      action:    'modify',
      orderId:   String(o.order_id || ''),
      qty:       Number(o.quantity) || 0,
      lotSize:   1,
      orderType: o.order_type || 'LIMIT',
      price:     o.price > 0 ? o.price : undefined,
      trigger:   o.trigger_price > 0 ? o.trigger_price : undefined,
      product:   o.product,
      account:   String(o.account || ''),
      accounts:  [],
    };
  }

  // Inline-action handlers — Cancel hits the broker directly; Modify
  // and Repeat open the orderTicketProps modal pre-filled.
  async function inlineCancel(/** @type {any} */ o, /** @type {Event} */ e) {
    e?.stopPropagation();
    try { await cancelOrder(o.order_id, o.account); await loadOrders(); }
    catch (err) { error = err.message || String(err); }
  }
  function inlineModify(/** @type {any} */ o, /** @type {Event} */ e) {
    e?.stopPropagation();
    orderTicketProps = _buildModifyProps(o);
  }
  // Inline-action handler — Reconcile: re-sync this single broker order
  // against the broker book and update the matching algo row when it
  // disagrees (postback miss, network drop, REJECTED chase that left
  // the algo row at OPEN, etc.).
  let _reconcilingId = $state(/** @type {string|null} */ (null));
  async function inlineReconcile(/** @type {any} */ o, /** @type {Event} */ e) {
    e?.stopPropagation();
    if (!o?.order_id || !o?.account) return;
    _reconcilingId = String(o.order_id);
    try {
      const res = await reconcileSingleOrder(o.order_id, o.account);
      if (res?.updated) await loadOrders();
    } catch (err) {
      error = err.message || String(err);
    } finally {
      _reconcilingId = null;
    }
  }

  // Empty-state copy adapts to which filter is active so the operator
  // never sees a generic "no orders" when they're actually looking at
  // a slice.
  const _emptyMessage = $derived.by(() => {
    const parts = [];
    if (filterStatus === 'all')          parts.push('orders');
    else if (filterStatus === 'open')    parts.push('OPEN orders');
    else if (filterStatus === 'complete')parts.push('FILLED orders');
    else if (filterStatus === 'rejected')parts.push('REJECTED orders');
    else if (filterStatus === 'cancelled') parts.push('CANCELLED orders');
    else parts.push('orders');
    if (_accountFilter.length === 1) parts.push(`for ${_accountFilter[0]}`);
    else if (_accountFilter.length > 1) parts.push(`for ${_accountFilter.length} accounts`);
    if (_exchangeFilter !== 'all') parts.push(`on ${_exchangeFilter}`);
    return `No ${parts.join(' ')} today.`;
  });

  onMount(() => {
    loadOrders();
    loadAccounts()
      .then((/** @type {any[]} */ list) => {
        _entryAccounts = (list || [])
          .map(a => String(a?.account_id || a?.account || a || ''))
          .filter(Boolean);
        // Re-resolve once the settings + accounts list have landed:
        //   recent → settings.default → first-loaded fallback.
        if (!_entryAccount) {
          _entryAccount = resolveAccount(_entryAccounts[0] || '');
        }
        if (!_entrySymbol) {
          const s = resolveSymbol();
          if (s) _entrySymbol = s;
        }
      })
      .catch(() => {});
    loadInstruments().catch(() => {});
    unsub = createPerformanceSocket((msg) => {
      // Either an order postback or a positions/holdings refresh — both
      // mean "re-fetch the order book." The card grid auto-updates from
      // the new `orders` state.
      if (msg.event === 'order_update' || msg.event === 'performance_updated') {
        _debouncedLoadOrders();
      }
    });
  });
  onDestroy(() => { unsub?.(); });
</script>

<svelte:head><title>Orders | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col oc-page-wrap">
<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Orders</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={loadOrders} loading={loading} label="orders" />
    <PageHeaderActions symbol={_entrySymbol} hideOrder={true} />
  </span>
</div>

{#if error}<div class="mb-1 p-1.5 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>{/if}

<!-- Status filter strip — compact one-line counters. Number + label
     sit inline (number left, label right) so each card collapses to
     ~38 px tall instead of the previous ~80 px (Phase D of the
     redesign). Card border carries the status colour now (Phase E)
     so the number stays uniformly slate. -->
<div class="grid grid-cols-5 gap-2 mt-1 mb-2">
  {#each [
    { id: 'all',       label: 'All',       count: orders.length, accent: 'inactive' },
    { id: 'open',      label: 'Open',      count: orders.filter(o => o.status === 'OPEN' || o.status === 'TRIGGER PENDING').length, accent: 'running' },
    { id: 'complete',  label: 'Filled',    count: orders.filter(o => o.status === 'COMPLETE').length,  accent: 'active' },
    { id: 'rejected',  label: 'Rejected',  count: orders.filter(o => o.status === 'REJECTED').length,  accent: 'error' },
    { id: 'cancelled', label: 'Cancelled', count: orders.filter(o => o.status === 'CANCELLED').length, accent: 'cancelled' },
  ] as f}
    <button type="button"
      onclick={() => { filterStatus = f.id; _activityTab = 'book'; _colActivity = false; }}
      class="oc-filter-card {filterStatus === f.id ? 'oc-filter-card-on' : ''}"
      data-status={f.accent}>
      <span class="oc-filter-count">{f.count}</span>
      <span class="oc-filter-label">{f.label}</span>
    </button>
  {/each}
</div>

<!-- Order Entry card — bucket-card chrome re-added per operator
     request. Has its own [Collapse · DefaultSize · Fullscreen] trio
     on the right. Tabs strip stays in the header alongside the
     section label + Symbol + Account picker. -->
<!-- Operator: "keep the bucket cards. the outer container ascent,
     borders, heading should be in sync with the current order entry
     card container in orders page". bucket-card-entry wrapper (amber
     left accent + navy gradient + "Order Entry" label) restored —
     just the INNER content (tabs / body / common-actions) comes
     from SymbolPanel and matches the modal's rendering exactly.
     `headerless` suppresses SymbolPanel's own header chip so the
     bucket-header's section label isn't duplicated. -->
<section class="bucket-card bucket-card-entry mt-1 mb-2"
  class:fs-card-on={_fsEntry}
  class:is-collapsed={_colEntry}>
  <!-- Operator: "make sure all the elements from order entry panel
       in orders modal should be present in order entry panel in
       orders page". Bucket-header now carries only the section
       label + card-control trio — SymbolPanel below renders its
       own internal picker row (Account · Type · Symbol · Exchange)
       + tab strip the same way the modal does. -->
  <!-- Operator: "the current orders row in order entry panel needs
       to be removed. In the header at ORDER ENTRY on the left
       side of the panel where full screen icon is present in the
       row". Bucket-header carries plain "ORDER ENTRY" label on
       the left (no chip / decoration) + card-control trio on the
       right. SymbolPanel below renders with `headerless` so its
       OWN internal header strip (the duplicate "ORDER ENTRY" chip
       + close button) is suppressed — operator wanted just one
       row, not two stacked. -->
  <div class="bucket-header oc-entry-header-bare">
    <span class="oc-entry-label">
      <svg class="oc-entry-icon" width="13" height="13" viewBox="0 0 16 16"
           fill="none" stroke="currentColor" stroke-width="1.5"
           stroke-linecap="round" aria-hidden="true">
        <rect x="3.2" y="2" width="9.6" height="12" rx="1.2" />
        <path d="M5.5 6h5M5.5 8.5h5M5.5 11h3" stroke-width="1.4" />
      </svg>
      ORDER ENTRY
    </span>
    <span class="oc-spacer"></span>
    {#if _fsEntry}
      <RefreshButton onClick={loadOrders} loading={loading} label="orders" />
    {/if}
    <CollapseButton bind:isCollapsed={_colEntry} label="Order Entry" />
    <DefaultSizeButton bind:isFullscreen={_fsEntry} bind:isCollapsed={_colEntry} label="Order Entry" />
    <FullscreenButton bind:isFullscreen={_fsEntry} label="Order Entry" />
  </div>
  <div class="card-body" hidden={_colEntry}>
    <!-- `headerless` re-added: SymbolPanel's internal header strip
         (title chip + close X) was the "current orders row" the
         operator asked to remove. The picker row (Account · Type ·
         Symbol · Exchange) still renders — that gate is
         `!headerless` standalone after the earlier change. -->
    <SymbolPanel
      inline
      headerless
      hideBottomPanel
      showCommonActions
      triggerSubmit={_triggerSubmit}
      triggerBasket={_triggerBasket}
      bind:activeTab={_entryActiveTab}
      defaultTab="chain"
      symbol={_entrySymbol}
      exchange={_entryExchange}
      account={_entryAccount}
      accounts={_entryAccounts}
      action="open"
      side="BUY"
      onSymbolChange={(sym) => { _entrySymbol = sym; }}
      onSubmit={(payload) => {
        if (payload?.mode === 'draft') return;
        loadOrders();
      }}
      onClose={() => { /* inline mode — no close affordance */ }} />
    {#if isDemo}
      <div class="mt-2 text-[0.62rem] text-[#7e97b8] font-mono">
        Demo: read-only — sign in to place orders
      </div>
    {/if}
  </div>
</section>

<!-- Chases in flight — every OPEN algo_orders row across paper /
     live / shadow. Per-row Kill button cancels the chase
     (paper-engine flip OR broker.cancel_order). Reusable card; see
     ChaseCard.svelte for the markup + polling. -->
<section class="bucket-card bucket-card-chase mb-2">
  <div class="bucket-header oc-chase-header">
    <span class="mp-section-label">Chases</span>
  </div>
  <div class="card-body oc-chase-body">
    <ChaseCard pollMs={3000} onKilled={() => loadOrders()} />
  </div>
</section>

<!-- Order Activity card — single card that absorbs both Order Book
     (the order grid that used to be its own card) and Order Log
     (UnifiedLog of order / agent events). Book is the default tab;
     Log is one click away. Order History was retired — it
     duplicated Order Book. -->
<section class="bucket-card bucket-card-activity oc-fill mb-2"
  class:fs-card-on={_fsActivity}
  class:is-collapsed={_colActivity}>
  <div class="bucket-header">
    <span class="mp-section-label">Order Activity</span>
    <AlgoTabs
      tabs={ACT_TABS.map(t => ({
        id: t.id,
        label: t.label,
        badge: t.id === 'book' && _filteredOrders.length > 0 ? _filteredOrders.length : undefined,
      }))}
      bind:value={_activityTab}
    />
    {#if _activityTab === 'book'}
      <!-- Book-only filters — Account multi-select + Exchange chips.
           Lifted from the retired Order Book card's header. -->
      {#if _availableAccounts.length > 1}
        <AccountMultiSelect
          bind:value={_accountFilter}
          options={_availableAccounts.map(a => ({ value: a, label: a }))} />
      {/if}
      {#if _availableExchanges.length > 1}
        <div class="oc-ex-strip" role="group" aria-label="Exchange filter">
          <button type="button" class="oc-chip" class:oc-chip-on={_exchangeFilter === 'all'}
            onclick={() => _exchangeFilter = 'all'}>All</button>
          {#each _availableExchanges as ex}
            <button type="button" class="oc-chip" class:oc-chip-on={_exchangeFilter === ex}
              onclick={() => _exchangeFilter = ex}>{ex}</button>
          {/each}
        </div>
      {/if}
    {/if}
    <span class="oc-spacer"></span>
    {#if _fsActivity}
      <RefreshButton onClick={loadOrders} loading={loading} label="activity" />
    {/if}
    <CollapseButton bind:isCollapsed={_colActivity} label="Order Activity" />
    <DefaultSizeButton bind:isFullscreen={_fsActivity} bind:isCollapsed={_colActivity} label="Order Activity" />
    <FullscreenButton bind:isFullscreen={_fsActivity} label="Order Activity" />
  </div>
  <div class="card-body oc-act-body" hidden={_colActivity}>
    {#if _activityTab === 'log'}
      <!-- Wrap in .oc-activity-log so the :global overrides above
           restyle UnifiedLog's card-mode rows to match /market's
           news-row look (flat row, time on the left, thin bottom
           divider, no left accent). -->
      <!-- UnifiedLog's own `.ul-wrap` carries `overflow-y: auto`, so we
           give it the flex height directly via `heightClass` instead of
           wrapping in a second `oc-act-scroll`. Previous nesting produced
           two scroll contexts where the inner never received a finite
           height — the log grew past the activity card and pushed the
           page footer over the last few rows. -->
      <div class="oc-activity-log oc-act-flex">
        <UnifiedLog
          filter={{ simMode: $executionMode === 'sim' ? true : undefined }}
          pollMs={3000}
          maxRows={50}
          heightClass="oc-act-log-scroll"
          cardMode={true}
          tsFormat="dual" />
      </div>
    {:else if loading && !orders.length}
      <div class="text-center text-muted text-xs animate-pulse py-2">Loading orders…</div>
    {:else if _filteredOrders.length}
      <div class="oc-book-grid">
        {#each _filteredOrders as o (o.order_id)}
          <OrderCard
            order={o}
            onCardClick={() => { selectedOrder = (selectedOrder?.order_id === o.order_id ? null : o); }}
            onSymbolClick={(ord, _e) => { orderTicketProps = { symbol: ord.tradingsymbol, exchange: ord.exchange || '', defaultTab: 'ticket' }; }}
            onSymbolContext={(ord, ev) => { _ctxMenu = { symbol: ord.tradingsymbol, exchange: ord.exchange || '', x: ev.clientX, y: ev.clientY }; }}>
            {#snippet actions(ord)}
              <div class="oc-row-actions">
                {#if isOpenStatus(ord.status)}
                  <button type="button" class="oc-act-btn" title="Modify order"
                    aria-label="Modify order" onclick={(e) => inlineModify(ord, e)}>
                    <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                      <path d="M11.5 3l1.5 1.5L5 12.5 2 13l.5-3L11.5 3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                  </button>
                  <button type="button" class="oc-act-btn oc-act-cancel" title="Cancel order"
                    aria-label="Cancel order" onclick={(e) => inlineCancel(ord, e)}>
                    <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                      <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                    </svg>
                  </button>
                {/if}
                <!-- Reconcile (circular arrow) — re-sync this single
                     order's algo row against the broker book. Visible
                     for every status, since stuck rows can be OPEN,
                     REJECTED-but-still-OPEN, or terminal-but-stale. -->
                <button type="button" class="oc-act-btn oc-act-reconcile"
                  title="Reconcile with broker"
                  aria-label="Reconcile with broker"
                  disabled={_reconcilingId === String(ord.order_id)}
                  onclick={(e) => inlineReconcile(ord, e)}>
                  <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                    <path d="M3 8a5 5 0 0 1 8.6-3.5M13 8a5 5 0 0 1-8.6 3.5"
                      fill="none" stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" />
                    <path d="M11.5 2v3h-3M4.5 14v-3h3"
                      fill="none" stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              </div>
            {/snippet}
          </OrderCard>
        {/each}
      </div>
    {:else}
      <div class="text-center text-muted text-xs py-2">{_emptyMessage}</div>
    {/if}
  </div>
</section>

<OrderDetail order={selectedOrder}
  onclose={() => selectedOrder = null}
  onchanged={async () => { await loadOrders(); if (selectedOrder) selectedOrder = orders.find(o => o.order_id === selectedOrder.order_id) || null; }}
  onmodify={(ord) => {
    if (!ord) return;
    orderTicketProps = _buildModifyProps(ord);
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

{#if _ctxMenu}
  <SymbolContextMenu
    symbol={_ctxMenu.symbol}
    exchange={_ctxMenu.exchange}
    x={_ctxMenu.x}
    y={_ctxMenu.y}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      _ctxAction = /** @type {any} */ (action);
      _ctxMenu = null;
    }}
  />
{/if}

{#if _ctxAction === 'chart'}
  <ChartModal
    symbol={_ctxSym}
    exchange={_ctxExch}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'place-order'}
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    onSubmit={() => {}}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'log'}
  <ActivityLogModal onClose={() => { _ctxAction = null; }} />
{/if}

<style>

  /* Outer page wrap — fills the viewport between the navbar/strip
     above and the footer so the Activity card can grow to take all
     remaining vertical space. Status filter strip + Order Entry
     card stay at their natural heights; Activity flexes.
     Uses `flex: 1` inside `.algo-content` (which is `display: flex;
     flex-direction: column`) so the wrap inherits the actual
     remaining content area — not a hard `calc(100vh - Nrem)`
     guess that over-counted algo-content's own padding, pushing
     the Activity card below algo-card's 100vh into the body's
     default-white background and visually shoving the footer "on
     top of" the wrap's last few rows. `min-height: 0` is the
     standard flex-child unlock so the inner flex chain (oc-act-body
     → oc-act-scroll / oc-book-grid) can resolve to finite height
     and the scroll containers' `overflow-y: auto` triggers. */
  .oc-page-wrap {
    flex: 1 1 0;
    min-height: 0;
    /* Hard-clip the wrap so a tall Entry card (Order Ticket /
       Option Chain expanded) + Activity card's min-height: 12rem can't
       overflow past the wrap's capped box and paint over the sticky
       footer. Inner scrolls (.oc-act-log-scroll, .oc-book-grid,
       OrderTicket's own scroll) absorb overflow per-card. */
    overflow: hidden;
  }
  .oc-fill {
    flex: 1 1 0;
    min-height: 12rem;
  }
  .oc-act-body {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .oc-act-scroll {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
  }
  /* Log tab — flex container + matching height class on UnifiedLog's
     own `.ul-wrap` so the inner native scroll receives a finite
     height. Single scroll context, no nested overflow. */
  .oc-act-flex {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  :global(.oc-act-flex .oc-act-log-scroll) {
    flex: 1 1 0;
    min-height: 0;
  }
  /* Book tab — order grid uses the same scroll container as the log
     tab so card height is consistent when flipping between them. */
  .oc-book-grid {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.5rem;
  }
  @media (min-width: 640px) {
    .oc-book-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 1024px) {
    .oc-book-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }

  /* Card chrome — full 1.5px white-10% box-border plus a 3px colored
     left-edge accent stripe per card type. Each card has its own
     identity colour so the operator can tell them apart at a glance
     without reading the section label:
       • Order Entry    → amber-400 (writing surface — operator action)
       • Order Activity → cyan-400  (live data stream)
       • Order Book     → green-400 (records / history)
     Industry analogue: Splunk panel side-stripe, Datadog widget
     accent, Bloomberg PRTU section identity bars. */
  .bucket-card {
    width: 100%;
    min-width: 0;
    padding: 0.55rem 0.65rem 0.6rem 0.8rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-left: 3px solid rgba(251, 191, 36, 0.70);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35),
                inset 0 1px 0 rgba(255, 255, 255, 0.06);
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
  }
  .bucket-card-activity { border-left-color: rgba(34, 211, 238, 0.75); }
  /* Order Entry card overrides `.bucket-card`'s inner padding to
     zero — SymbolPanel's picker / tabs / body / common-actions
     each define their own horizontal inset, the same way they do
     inside the modal panel chrome. Operator: "if you keep order
     ticket specific content in card, it will match order modal".
     Card-body still ships a small inner bottom buffer so the
     common-actions row doesn't slam against the bucket-card border. */
  .bucket-card-entry { padding: 0 0 0.4rem 0; }
  .bucket-card-entry > .card-body { padding: 0; }
  /* Chase card — rose accent so it reads as the "kill" surface
     alongside amber Entry and cyan Activity. Section label inherits
     the same rose via the cc-label rule inside ChaseCard. */
  .bucket-card-chase { border-left-color: rgba(248, 113, 133, 0.70); }
  /* Hide the outer chase card chrome when ChaseCard renders nothing
     (no active chases). Avoids an empty rose-bordered band on idle
     pages. Operator: "if there are no active chases, no need to
     show 'no active chases'". */
  .bucket-card-chase:not(:has(.cc-root)) { display: none; }
  .oc-chase-header { margin-bottom: 0.25rem; }
  .oc-chase-body { padding: 0; }
  :global(.oc-chase-body .cc-root) { width: 100%; }
  /* bucket-card-book retired (Order Book merged into Activity card). */

  .bucket-header { margin-bottom: 0.35rem; }
  /* .mp-section-label is defined globally in app.css. */
  /* Match each card's section-label colour to its left-edge accent. */
  .bucket-card-activity .mp-section-label { color: rgba(34, 211, 238, 0.85); }
  /* Operator: "order entry card header in order page and modal should
     be more prominent". Bumps the section label to a chip-style amber
     pill so the "ORDER ENTRY" reads at a glance from across the
     screen. Activity card label scoped separately so it keeps its
     section-specific cyan. */
  .bucket-card-entry .mp-section-label {
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.16);
    border: 1px solid var(--algo-amber-border);
    padding: 0.22rem 0.55rem;
    border-radius: 4px;
    box-shadow: 0 1px 4px rgba(251, 191, 36, 0.18);
  }

  /* History list — retired with the History tab. Style block kept
     empty for callsite stability but no element uses it. */

  /* UnifiedLog rows inside the Activity card — flat news-row look.
     Row is a `display: block` container with INLINE children so the
     time chip, kind chip, refs, and message all flow as inline text.
     When the message wraps on a narrow viewport, the second line
     continues at the LEFT edge (no indent) — the previous flex-row
     layout indented wrapped text by the gap, wasting horizontal
     space on mobile. Operator request: "data should continue with
     no indentation". */
  :global(.oc-activity-log .ul-list-cards) {
    gap: 0;
    padding: 0;
    background: transparent;
  }
  :global(.oc-activity-log .ul-card) {
    display: block;
    background: transparent;
    border: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 0;
    padding: 0.32rem 0.25rem;
    line-height: 1.4;
  }
  :global(.oc-activity-log .ul-card:last-child) { border-bottom: 0; }
  :global(.oc-activity-log .ul-card:hover) {
    background: rgba(255, 255, 255, 0.02);
  }
  /* Head + msg become inline-block so they sit side-by-side when
     space permits, but the msg wraps to the article's left edge —
     not the head's right edge — when there's not enough width. */
  :global(.oc-activity-log .ul-card-head) {
    display: inline;
    font-size: 0.6rem;
  }
  :global(.oc-activity-log .ul-card-head > *) {
    margin-right: 0.35rem;
  }
  :global(.oc-activity-log .ul-card-time) {
    font-size: 0.6rem;
    letter-spacing: 0.02em;
    margin-left: 0;
    margin-right: 0.4rem;
  }
  :global(.oc-activity-log .ul-card-msg) {
    display: inline;
    color: #e2e8f0;
    font-weight: 500;
    font-size: 0.66rem;
    padding-left: 0;
    margin-left: 0;
    word-break: break-word;
  }
  /* Order History tab dead CSS removed (~50 lines): oc-act-empty,
     oc-act-head[-done], oc-act-count, oc-act-row[-done],
     oc-act-status[-pending|-complete|-rejected|-cancelled],
     oc-act-side, oc-act-qty, oc-act-sym, oc-act-px, oc-act-meta. */

  /* Flex spacer pushes the bucket-header's [Collapse · DefaultSize ·
     Fullscreen] trio to the card's right edge. Matches the
     `.cap-eq-spacer` pattern on /dashboard. */
  .oc-spacer { flex: 1 1 0; }

  /* Common action footer at the bottom of the Order Entry card.
     Mode pills · Exit · +Basket · Side · BUY/SELL submit. The
     buttons dispatch to whichever tab is active (Ticket / Chain /
     Command) so the operator always sees the same row of
     affordances regardless of which entry channel they're using. */
  /* .oc-actions / .oc-mode-pill / .oc-act-{exit,basket,side,submit}
     CSS retired — the page-level custom footer was replaced by
     SymbolPanel's shared .oes-common-actions block so both surfaces
     (modal + /orders) read from one component. ~80 lines dropped. */

  /* Flat section header — only the card-control trio rides here
     now (Collapse / DefaultSize / Fullscreen). Section label +
     other decorations removed per operator request. */
  /* Flat section header — display row matching every other bucket-card.
     `.oc-entry-header` merged into this rule (same semantics, always
     co-applied). Small inset padding so the label + collapse trio don't
     slam against the bucket-card frame. */
  .oc-entry-header-bare {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin: 0;
    min-height: 1.4rem;
    padding: 0.35rem 0.5rem;
  }
  /* "ORDER ENTRY" label — matches the page's `.mp-section-label`
     convention used by Chase + Activity cards (0.6rem, weight 700,
     amber-70%, 0.08em letter-spacing). */
  .oc-entry-label {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(251, 191, 36, 0.7);
    flex-shrink: 0;
  }
  .oc-entry-icon { color: currentColor; flex-shrink: 0; width: 11px; height: 11px; }
  /* Header — plain row matching every other bucket-card on the page.
     Small inset padding so the label + collapse/fullscreen trio
     don't slam against the bucket-card frame (since
     `.bucket-card-entry` has its outer padding zeroed for the
     SymbolPanel content alignment). */
  /* Status filter cards — polished chrome with richer gradients +
     inner highlight + soft outer shadow. Each card carries:
       • two-stop status-tinted gradient (richer than the previous
         linear blend; goes from a brighter top edge to a darker
         bottom edge for depth)
       • status-colored border (~60% opacity)
       • status-colored count number (1.25rem)
       • inset highlight at the top edge (~1px white-8%) for the
         "lit from above" feel every modern fintech UI carries
         (IBKR, ToS, TradingView, Sensibull)
     Hover bumps the border to 80% opacity + lifts the card 1px.
     Selected adds a 2px amber inset ring on top of the card's own
     status colour — unambiguous active-filter cue. */
  .oc-filter-card {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.2rem;
    padding: 0.6rem 0.5rem;
    background:
      linear-gradient(180deg,
        rgba(255, 255, 255, 0.04) 0%,
        rgba(255, 255, 255, 0.00) 30%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 5px;
    box-shadow:
      0 1px 0 rgba(255, 255, 255, 0.06) inset,
      0 2px 4px rgba(0, 0, 0, 0.25);
    color: var(--algo-slate);
    font-family: ui-monospace, monospace;
    cursor: pointer;
    transition: border-color 0.12s, transform 0.12s, box-shadow 0.12s;
  }
  .oc-filter-card:hover {
    border-color: rgba(255, 255, 255, 0.30);
    transform: translateY(-1px);
  }

  /* Status-tinted backgrounds + borders. Two-stop gradient (status
     hue at 14% top → 4% bottom) over the base navy. The 8% black
     bottom-cap from .oc-filter-card layers on top so even tinted
     cards keep the depth cue. */
  .oc-filter-card[data-status="running"] {
    background:
      linear-gradient(180deg,
        rgba(251, 191, 36, 0.18) 0%,
        rgba(251, 191, 36, 0.06) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(251, 191, 36, 0.60);
  }
  .oc-filter-card[data-status="active"] {
    background:
      linear-gradient(180deg,
        rgba(74, 222, 128, 0.18) 0%,
        var(--algo-green-bg-soft) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(74, 222, 128, 0.60);
  }
  .oc-filter-card[data-status="error"] {
    background:
      linear-gradient(180deg,
        rgba(248, 113, 113, 0.18) 0%,
        var(--algo-red-bg-soft) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: var(--algo-red-border);
  }
  /* Cancelled stays in the orange family — distinct from rejected
     red so the operator can tell the two terminal-error buckets
     apart. Industry convention: red = broker rejection (something
     went wrong); orange = operator/system cancel (intent
     withdrawal). */
  .oc-filter-card[data-status="cancelled"] {
    background:
      linear-gradient(180deg,
        rgba(251, 146, 60, 0.18) 0%,
        rgba(251, 146, 60, 0.06) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(251, 146, 60, 0.55);
  }
  .oc-filter-card[data-status="inactive"] {
    border-color: rgba(126, 151, 184, 0.45);
  }

  /* Selected state — bright amber inset ring stacked on top of the
     status colour. Shadow stack: amber ring + the lit-from-above
     highlight + a stronger outer drop so the active card visibly
     lifts. */
  .oc-filter-card-on {
    box-shadow:
      0 0 0 2px rgba(251, 191, 36, 0.65) inset,
      0 1px 0 rgba(255, 255, 255, 0.10) inset,
      0 3px 6px rgba(0, 0, 0, 0.35);
    transform: translateY(-1px);
  }

  /* Count number — bigger + colour-coded by status. */
  .oc-filter-count {
    font-weight: 800;
    font-size: 1.3rem;
    line-height: 1;
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
  }
  .oc-filter-card[data-status="running"]   .oc-filter-count { color: #fbbf24; }
  .oc-filter-card[data-status="active"]    .oc-filter-count { color: #4ade80; }
  .oc-filter-card[data-status="error"]     .oc-filter-count { color: #f87171; }
  .oc-filter-card[data-status="cancelled"] .oc-filter-count { color: #fb923c; }

  .oc-filter-label {
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-muted);
  }

  /* oc-count retired with the standalone Order Book card header. */


  /* Exchange filter strip inside the Order Book card header.
     Compact pill cluster, same cyan-400 accent family as the
     card-control trio so the filter affordances read as one
     consistent control language. */
  .oc-ex-strip {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
  }
  .oc-chip {
    padding: 0.05rem 0.4rem;
    background: var(--algo-cyan-bg-soft);
    border: 1px solid var(--algo-cyan-border-soft);
    border-radius: 8px;
    color: var(--algo-muted);
    font-size: 0.55rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .oc-chip:hover {
    background: rgba(34, 211, 238, 0.16);
    color: var(--algo-slate);
    border-color: var(--algo-cyan-border);
  }
  .oc-chip-on {
    background: rgba(34, 211, 238, 0.26);
    color: #67e8f9;
    border-color: rgba(34, 211, 238, 0.85);
  }

  /* Per-row action strip — Modify / Cancel for OPEN; Repeat for
     terminal. Sits on the right edge of each order card. Cyan-400
     palette + 1.2rem squares so the icons read as a smaller-scale
     family of the card-control trio above (which is 1.4rem). */
  .order-card { position: relative; }

  /* Symbol text as a clickable affordance — underline on hover so
     the operator knows it's interactive. Colour stays the same
     sky-blue (var(--algo-slate)) as the surrounding text; underline is the
     only extra cue so the row stays scan-tight. */
  :global(.oc-sym-btn) {
    cursor: pointer;
    border-radius: 2px;
    transition: color 0.1s, background 0.1s;
  }
  :global(.oc-sym-btn:hover) {
    color: #7dd3fc !important;
    text-decoration: underline;
  }

  .oc-row-actions {
    position: absolute;
    top: 0.25rem;
    right: 0.25rem;
    display: inline-flex;
    gap: 0.2rem;
    opacity: 0.65;
    transition: opacity 0.12s;
  }
  .order-card:hover .oc-row-actions { opacity: 1; }
  .oc-act-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.2rem;
    height: 1.2rem;
    padding: 0;
    background: var(--algo-cyan-bg);
    border: 1px solid var(--algo-cyan-border);
    border-radius: 3px;
    color: #22d3ee;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
  }
  .oc-act-btn:hover {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.85);
    color: #67e8f9;
  }
  /* Cancel button shifts to red-400 palette to signal destructiveness
     without breaking the visual family — same shape + size + accent
     pattern, only the hue changes. */
  .oc-act-cancel {
    background: rgba(248, 113, 113, 0.14);
    border-color: var(--algo-red-border);
    color: #f87171;
  }
  .oc-act-cancel:hover {
    background: rgba(248, 113, 113, 0.26);
    border-color: rgba(248, 113, 113, 0.85);
    color: #fca5a5;
  }
  /* Reconcile button — sky-blue palette so it reads as "sync / refresh"
     rather than "edit" (amber) or "destroy" (red). */
  .oc-act-reconcile {
    background: rgba(125, 211, 252, 0.14);
    border-color: rgba(125, 211, 252, 0.55);
    color: #7dd3fc;
  }
  .oc-act-reconcile:hover {
    background: rgba(125, 211, 252, 0.26);
    border-color: rgba(125, 211, 252, 0.85);
    color: #bae6fd;
  }
  .oc-act-reconcile:disabled {
    opacity: 0.5;
    cursor: progress;
  }

</style>
