<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { nowStamp, logTimeIst, formatDualTz } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import { fetchOrders, cancelOrder } from '$lib/api';
  import OrderDetail from '$lib/OrderDetail.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import { loadInstruments } from '$lib/data/instruments';
  import { priceFmt, qtyFmt } from '$lib/format';
  import { loadAccounts } from '$lib/data/accounts';
  import Select from '$lib/Select.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';
  import { executionMode } from '$lib/stores';
  import { createPerformanceSocket } from '$lib/ws';
  import ChartModal from '$lib/ChartModal.svelte';
  import { longPress } from '$lib/actions/longPress.js';
  import { ORDER_TABS } from '$lib/order/tabs.js';

  // Row-level chart modal — distinct from any header-level chart state.
  let _rowChartModalSym  = $state('');
  let _rowChartModalExch = $state('');
  function _openRowChart(/** @type {string} */ symbol, /** @type {string} */ exchange = '') {
    _rowChartModalSym  = String(symbol  || '').toUpperCase();
    _rowChartModalExch = String(exchange || '');
  }

  // Tab strip metadata — sourced from ORDER_TABS ($lib/order/tabs.js)
  // with visual palette layered on. Chart tab removed — chart now lives
  // in ChartModal (icon button next to symbol picker).
  // Chain first — basket builder is the most-used surface per operator.
  const TABS = ORDER_TABS.map(t => ({
    ...t,
    ...(t.id === 'chain'   ? { dot: '#4ade80', activeTxt: '#4ade80', activeBorder: '#4ade80', activeBg: 'rgba(74,222,128,0.14)'  } :
        t.id === 'ticket'  ? { dot: '#fbbf24', activeTxt: '#fbbf24', activeBorder: '#fbbf24', activeBg: 'rgba(251,191,36,0.14)'  } :
                             { dot: '#7dd3fc', activeTxt: '#7dd3fc', activeBorder: '#7dd3fc', activeBg: 'rgba(125,211,252,0.14)' }),
  }));

  // Activity-card tabs share the same shape + style as the Entry-
  // card tabs above (single oc-tab class) so the two tab strips
  // read as one consistent vocabulary across the page. Each tab
  // carries its own colour for the active underline + text.
  const ACT_TABS = /** @type {const} */ ([
    { id: 'log',  label: 'Agents', activeTxt: '#7dd3fc', activeBorder: '#7dd3fc', activeBg: 'rgba(125,211,252,0.14)' },
    { id: 'book', label: 'Orders', activeTxt: '#4ade80', activeBorder: '#4ade80', activeBg: 'rgba(74,222,128,0.14)' },
  ]);

  let orders        = $state([]);
  let loading       = $state(true);
  let error         = $state('');
  // Default the status filter to ALL — matches every other LogPanel
  // mount (Activity modal, Order modal bottom panel, /console, /agents)
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

  // Page-level Symbol picker for the Order Entry card. Sits in the
  // bucket-header right after the "Order Entry" label, and we pass
  // its value down to the inline SymbolPanel via the `symbol` prop.
  // SymbolPanel's `headerless={true}` flag skips the shell's own
  // copy of this picker so the operator sees one chip, not two.
  let _entrySymbol     = $state('');

  // Page-level shared state for the Order Entry shell.
  let _entryAccount = $state('');
  // Default to 'chain' — basket-building option chain is the most-used
  // surface per operator. Ticket / Command are one click away.
  let _entryActiveTab = $state(/** @type {'chain'|'ticket'} */ ('chain'));
  let _entryAccounts  = $state(/** @type {string[]} */ ([]));

  // Page-level chart modal state — opened by the chart-icon button next
  // to the symbol picker in the Order Entry bucket-header.
  let _orderChartModalOpen = $state(false);

  // Common action footer — Mode pills + Exit + +Basket + BUY/SELL
  // submit live at the page level (not inside each tab) so the
  // operator sees the same affordances regardless of which tab
  // (Ticket / Chain / Command) is active.
  let _commonMode = $state(/** @type {'paper'|'live'|'shadow'|'sim'|'replay'} */ (
    /** @type {any} */ (executionMode).get?.() || 'paper'
  ));
  executionMode.subscribe(v => { _commonMode = /** @type {any} */ (v) || 'paper'; });
  // Counter-prop dispatch. The footer increments these; the
  // OrderTicket inside SymbolPanel reacts via $effect.
  let _triggerSubmit = $state(0);
  let _triggerBasket = $state(0);
  // Tracks the operator's intended side (BUY/SELL) for the common
  // submit button label. Updated when the operator clicks the side
  // toggle inside the active tab — currently surfaces only as a
  // sticky label hint since the Ticket form owns its own _side
  // state. Defaults BUY; flipped by the page-level Side pill below.
  let _commonSide = $state(/** @type {'BUY'|'SELL'} */ ('BUY'));

  function _fireSubmit() {
    if (_entryActiveTab === 'ticket') { _triggerSubmit++; return; }
    // Other tabs handle submit via their own widgets for now.
    // CommandLineTab: Enter key. ChainTab: in-card buttons.
  }
  function _fireBasket() {
    if (_entryActiveTab === 'ticket') { _triggerBasket++; return; }
  }
  function _fireExit() {
    // Reset the entry symbol — closes the operator's working state.
    _entrySymbol = '';
  }

  // Per-card collapse + fullscreen state. No persistence (no cardId
  // on CollapseButton) so every page load opens both cards expanded
  // — matches the dashboard pattern.
  let _colEntry    = $state(false);
  let _fsEntry     = $state(false);
  let _colActivity = $state(false);
  let _fsActivity  = $state(false);

  // Activity-card tab state. Order Book (card grid) is the default —
  // matches the LogPanel Orders tab format shown in every other
  // surface (Activity modal, Order modal bottom panel, /console,
  // /agents). Agent Log (UnifiedLog of recent events) is one click
  // away. Earlier 'log' was the default but that left the operator
  // landing on a different visual format than every other Orders
  // surface uses.
  let _activityTab = $state(/** @type {'log'|'book'} */ ('book'));

  // Chain tab is disabled for cash equity (no FUT/CE/PE suffix). Same
  // logic SymbolPanel uses internally; duplicated here so the tab
  // strip in our bucket-header knows when to grey out the Chain pill.
  const _chainDisabled = $derived.by(() => {
    const s = String(_entrySymbol || '').toUpperCase();
    if (!s) return false; // no symbol picked yet — leave Chain clickable
    return !/(?:CE|PE|FUT)$/.test(s);
  });
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

  // Status check used by per-row action gating.
  const isOpenStatus = (/** @type {string} */ s) =>
    s === 'OPEN' || s === 'TRIGGER PENDING';

  // Slippage on a filled order = avg_price − limit_price. Signed
  // (positive = paid more / received less than asked); null when the
  // order is unfilled or has no limit price (MARKET orders).
  const slippage = (/** @type {any} */ o) => {
    if (o.status !== 'COMPLETE') return null;
    if (o.average_price == null || o.price == null) return null;
    const p = Number(o.price);
    if (!(p > 0)) return null;
    const d = Number(o.average_price) - p;
    return Number.isFinite(d) ? d : null;
  };

  // Tag colour-coding — ramboq-ticket = blue (manual operator
  // placement), ramboq-agent* = amber (algo / agent-fired). Anything
  // else stays neutral.
  const tagClass = (/** @type {string} */ tag) => {
    if (!tag) return '';
    if (tag === 'ramboq-ticket') return 'tag-manual';
    if (tag.startsWith('ramboq-agent')) return 'tag-agent';
    return '';
  };

  // Available account / exchange values for the filter chips.
  const _availableAccounts = $derived([...new Set(orders.map(o => o.account).filter(Boolean))]);
  const _availableExchanges = $derived([...new Set(orders.map(o => o.exchange).filter(Boolean))]);

  // Single source of truth for which orders the Book card shows.
  // Combines status + account + exchange filters in one pass so the
  // per-status count chips on the filter strip stay accurate.
  const _filteredOrders = $derived.by(() => {
    return orders.filter(o => {
      const s = o.status;
      if (filterStatus === 'open' && !isOpenStatus(s)) return false;
      if (filterStatus !== 'all' && filterStatus !== 'open' && s !== filterStatus.toUpperCase()) return false;
      if (_accountFilter.length && !_accountFilter.includes(o.account)) return false;
      if (_exchangeFilter !== 'all' && o.exchange !== _exchangeFilter) return false;
      return true;
    });
  });

  // Inline-action handlers — Cancel hits the broker directly; Modify
  // and Repeat open the orderTicketProps modal pre-filled.
  async function inlineCancel(/** @type {any} */ o, /** @type {Event} */ e) {
    e?.stopPropagation();
    try { await cancelOrder(o.order_id, o.account); await loadOrders(); }
    catch (err) { error = err.message || String(err); }
  }
  function inlineModify(/** @type {any} */ o, /** @type {Event} */ e) {
    e?.stopPropagation();
    orderTicketProps = {
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
      defaultMode:    'paper',
      availableModes: ['paper'],
    };
  }
  function inlineRepeat(/** @type {any} */ o, /** @type {Event} */ e) {
    e?.stopPropagation();
    orderTicketProps = {
      symbol:    String(o.tradingsymbol || '').toUpperCase(),
      exchange:  o.exchange || 'NFO',
      side:      o.transaction_type,
      action:    'open',
      qty:       Number(o.quantity) || 0,
      lotSize:   1,
      orderType: o.order_type || 'LIMIT',
      price:     o.price > 0 ? o.price : undefined,
      trigger:   o.trigger_price > 0 ? o.trigger_price : undefined,
      product:   o.product,
      account:   String(o.account || ''),
      accounts:  [],
      defaultMode:    'live',
      availableModes: ['live'],
    };
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
        if (!_entryAccount && _entryAccounts.length === 1) _entryAccount = _entryAccounts[0];
      })
      .catch(() => {});
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
<section class="bucket-card bucket-card-entry mt-1 mb-2"
  class:fs-card-on={_fsEntry}
  class:is-collapsed={_colEntry}>
  <div class="bucket-header oc-entry-header">
    <span class="mp-section-label">Order Entry</span>
    <!-- Symbol picker — sits IMMEDIATELY after the section label per
         operator request. SymbolPanel below renders `headerless` so
         its own copy of this picker is suppressed. -->
    <SymbolSearchInput
      bind:value={_entrySymbol}
      placeholder="Symbol…"
      onPick={(sym) => { _entrySymbol = sym; }}
      ariaLabel="Order entry symbol search" />
    <!-- Chart icon button — opens ChartModal for the current entry
         symbol. Same cyan-400 palette + 1.4rem sizing as the
         card-control trio. Disabled when no symbol is picked. -->
    <button type="button" class="oc-chart-btn"
            disabled={!_entrySymbol}
            title={_entrySymbol ? `Open chart for ${_entrySymbol}` : 'Pick a symbol first'}
            aria-label={_entrySymbol ? `Open chart for ${_entrySymbol}` : 'Open chart'}
            onclick={() => _orderChartModalOpen = true}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M2 13h12M3 11l3-4 3 2 4-6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>
    <!-- Page-level Account picker — every shell tab (Ticket, Chain,
         Command) inherits this. Each tab can still override locally
         (the Ticket form's Account select stays for per-ticket flips;
         Command Line accepts an account token to override for that
         one command). -->
    {#if _entryAccounts.length > 1}
      <div class="oc-entry-account">
        <Select bind:value={_entryAccount}
                placeholder="Account…"
                ariaLabel="Order entry account"
                options={_entryAccounts.map(a => ({ value: a, label: a }))} />
      </div>
    {:else if _entryAccounts.length === 1}
      <span class="oc-entry-acct-chip" title="Single broker account">{_entryAccounts[0]}</span>
    {/if}
    <!-- Tab strip lifted out of SymbolPanel and into the bucket-header
         per operator request "move the tabs to the row with text
         order entry". Same colour palette and dot indicators as
         SymbolPanel's internal strip. Active tab pushes through the
         bound activeTab prop to flip the shell body below. -->
    <div class="oc-tabs" role="tablist">
      {#each TABS as tab}
        {@const disabled = tab.id === 'chain' && _chainDisabled}
        {@const isActive = _entryActiveTab === tab.id}
        <button type="button" role="tab"
          class="oc-tab"
          class:oc-tab-disabled={disabled}
          disabled={disabled}
          title={disabled ? 'Chain tab applies to F&O instruments only' : undefined}
          aria-selected={isActive}
          aria-disabled={disabled}
          style="
            color: {isActive ? tab.activeTxt : '#94a3b8'};
            background: {isActive ? tab.activeBg : 'transparent'};
            border-bottom-color: {isActive ? tab.activeBorder : 'transparent'};
            font-weight: {isActive ? '800' : '600'};
            opacity: {disabled ? '0.5' : '1'};
          "
          onclick={() => { if (!disabled) _entryActiveTab = /** @type {any} */ (tab.id); }}>
          {tab.label}
        </button>
      {/each}
    </div>
    <span class="oc-spacer"></span>
    {#if _fsEntry}
      <RefreshButton onClick={loadOrders} loading={loading} label="orders" />
    {/if}
    <CollapseButton bind:isCollapsed={_colEntry} label="Order Entry" />
    <DefaultSizeButton bind:isFullscreen={_fsEntry} bind:isCollapsed={_colEntry} label="Order Entry" />
    <FullscreenButton bind:isFullscreen={_fsEntry} label="Order Entry" />
  </div>
  <div class="card-body" hidden={_colEntry}>
    <!-- 3-tab inline shell (Order Ticket default · Chain · Command).
         `headerless={true}` suppresses the shell's own symbol
         picker — the bucket-header above carries it. The
         `onSymbolChange` callback is unused here (the chain tab
         doesn't surface a way to re-pick from inside the shell)
         but reserved for future tab-internal picks. -->
    <SymbolPanel
      inline
      headerless
      tabsExternal
      hideBottomPanel
      actionsHidden
      showCommonActions={false}
      triggerSubmit={_triggerSubmit}
      triggerBasket={_triggerBasket}
      bind:activeTab={_entryActiveTab}
      defaultTab="chain"
      symbol={_entrySymbol}
      account={_entryAccount}
      accounts={_entryAccounts}
      action="open"
      side="BUY"
      onSymbolChange={(sym) => { _entrySymbol = sym; }}
      onSubmit={(payload) => {
        // Drafts are page-local — no broker write. PAPER / LIVE submits
        // hit the backend; the Order Book card below picks them up via
        // the WebSocket order_update postback (or this defensive refresh).
        if (payload?.mode === 'draft') return;
        loadOrders();
      }}
      onClose={() => { /* inline mode — no close affordance */ }} />
    {#if isDemo}
      <div class="mt-2 text-[0.62rem] text-[#7e97b8] font-mono">
        Demo: read-only — sign in to place orders
      </div>
    {/if}
    <!-- Common action footer — Mode pills + Exit + +Basket + BUY/SELL
         submit. Lives at the page level (not inside each tab body) so
         every order entry channel (Order Ticket / Chain / Command Line)
         sees the same affordances. The buttons dispatch to the active
         tab via counter-prop signalling. -->
    <div class="oc-actions">
      <div class="oc-actions-mode" role="group" aria-label="Execution mode">
        {#each /** @type {const} */ (['paper','live','shadow','sim','replay']) as m}
          <button type="button" class="oc-mode-pill"
            class:on={_commonMode === m}
            title={m === 'paper' ? 'Paper — risk-free engine, no broker hit.'
                  : m === 'live' ? 'Live — real broker order.'
                  : m === 'shadow' ? 'Shadow — captures Kite payload + margin check, no execution.'
                  : m === 'sim' ? 'Simulator — fabricated scenario book.'
                  : 'Replay — historical candle replay.'}
            onclick={() => executionMode.set(/** @type {any} */ (m))}>{m.toUpperCase()}</button>
        {/each}
      </div>
      <span class="oc-actions-spacer"></span>
      <button type="button" class="oc-act-exit"
        title="Clear the entry shell"
        onclick={_fireExit}>Exit</button>
      <button type="button" class="oc-act-basket"
        title="Add the current order to the basket — submit all together via the BUY/SELL button"
        onclick={_fireBasket}>+ Basket</button>
      <button type="button" class="oc-act-side"
        title="Flip side BUY ↔ SELL"
        onclick={() => _commonSide = _commonSide === 'BUY' ? 'SELL' : 'BUY'}>
        {_commonSide}
      </button>
      <button type="button"
        class="oc-act-submit"
        class:oc-act-buy={_commonSide === 'BUY'}
        class:oc-act-sell={_commonSide === 'SELL'}
        title="Place the order via the active tab"
        onclick={_fireSubmit}>
        Place {_commonSide.toLowerCase()}
      </button>
    </div>
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
    <div class="oc-tabs" role="tablist">
      {#each ACT_TABS as tab}
        {@const isActive = _activityTab === tab.id}
        <button type="button" role="tab"
          class="oc-tab"
          aria-selected={isActive}
          style="
            color: {isActive ? tab.activeTxt : '#94a3b8'};
            background: {isActive ? tab.activeBg : 'transparent'};
            border-bottom-color: {isActive ? tab.activeBorder : 'transparent'};
            font-weight: {isActive ? '800' : '600'};
          "
          onclick={() => _activityTab = /** @type {any} */ (tab.id)}>
          {tab.label}
          {#if tab.id === 'book' && _filteredOrders.length > 0}
            <span class="oc-act-badge">{_filteredOrders.length}</span>
          {/if}
        </button>
      {/each}
    </div>
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
      <div class="oc-activity-log oc-act-scroll">
        <UnifiedLog
          filter={{}}
          pollMs={3000}
          maxRows={50}
          heightClass=""
          cardMode={true}
          tsFormat="dual" />
      </div>
    {:else if loading && !orders.length}
      <div class="text-center text-muted text-xs animate-pulse py-2">Loading orders…</div>
    {:else if _filteredOrders.length}
      <div class="oc-book-grid">
        {#each _filteredOrders as o (o.order_id)}
          <!-- Outer is a div role=button (not <button>) so the inline
               Cancel / Modify / Repeat <button> elements can nest
               without invalid HTML. Click + Enter / Space toggle the
               OrderDetail panel; the inline actions stopPropagation
               so they don't double-fire the toggle. -->
          <div role="button" tabindex="0"
            onclick={() => selectedOrder = (selectedOrder?.order_id === o.order_id ? null : o)}
            onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectedOrder = (selectedOrder?.order_id === o.order_id ? null : o); } }}
            class="algo-status-card text-left p-2.5 transition order-card"
            data-status={statusDataAttr(o.status)}>
            <div class="flex items-center justify-between mb-0.5 gap-1">
              <span class="font-semibold text-xs"><span style="{txnColor(o.transaction_type)}">{o.transaction_type}</span> <span class="{acctColor(o.account)}">{o.account}</span> <!-- svelte-ignore a11y_interactive_supports_focus --><span
                  class="text-[#c8d8f0] oc-sym-btn"
                  role="button"
                  tabindex="0"
                  title="Open {o.tradingsymbol}"
                  onclick={(e) => { e.stopPropagation(); orderTicketProps = { symbol: o.tradingsymbol, exchange: o.exchange || '' }; }}
                  onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); orderTicketProps = { symbol: o.tradingsymbol, exchange: o.exchange || '' }; } }}
                  oncontextmenu={(ev) => { ev.preventDefault(); _ctxMenu = { symbol: o.tradingsymbol, exchange: o.exchange || '', x: ev.clientX, y: ev.clientY }; }}
                  use:longPress={(ev) => { _ctxMenu = { symbol: o.tradingsymbol, exchange: o.exchange || '', x: ev.clientX, y: ev.clientY }; }}
                >{o.tradingsymbol}</span></span>
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
              {#if o.exchange}<span class="log-chip"><span class="log-chip-key">ex:</span>{o.exchange}</span>{/if}
              <span class="log-chip"><span class="log-chip-key">qty:</span>{qtyFmt(o.filled_quantity)}/{qtyFmt(o.quantity)}</span>
              <span class="log-chip"><span class="log-chip-key">type:</span>{o.order_type}</span>
              <span class="log-chip"><span class="log-chip-key">price:</span>{o.average_price != null ? priceFmt(o.average_price) : o.price != null ? priceFmt(o.price) : '—'}</span>
              {#if slippage(o) != null}<span class="log-chip log-chip-slip" class:slip-up={slippage(o) > 0} class:slip-down={slippage(o) < 0}><span class="log-chip-key">slip:</span>{slippage(o) > 0 ? '+' : ''}{priceFmt(slippage(o))}</span>{/if}
              {#if o.trigger_price}<span class="log-chip"><span class="log-chip-key">trigger:</span>{priceFmt(o.trigger_price)}</span>{/if}
              {#if o.validity}<span class="log-chip"><span class="log-chip-key">validity:</span>{o.validity}</span>{/if}
              <span class="log-chip"><span class="log-chip-key">product:</span>{o.product}</span>
              <span class="log-chip"><span class="log-chip-key">variety:</span>{o.variety}</span>
              {#if o.order_timestamp}<span class="log-chip"><span class="log-chip-key">time:</span>{formatDualTz(new Date(o.order_timestamp))}</span>{/if}
              {#if o.tag}<span class="log-chip {tagClass(o.tag)}"><span class="log-chip-key">tag:</span>{o.tag}</span>{/if}
              {#if o.status_message}<span class="log-chip"><span class="log-chip-key">note:</span>{o.status_message}</span>{/if}
            </div>
            <!-- Inline action strip — Cancel/Modify for live orders,
                 Repeat for terminal ones. Cyan-400 palette matching
                 the card-control trio family. -->
            <div class="oc-row-actions">
              {#if isOpenStatus(o.status)}
                <button type="button" class="oc-act-btn" title="Modify order"
                  aria-label="Modify order" onclick={(e) => inlineModify(o, e)}>
                  <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                    <path d="M11.5 3l1.5 1.5L5 12.5 2 13l.5-3L11.5 3z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </button>
                <button type="button" class="oc-act-btn oc-act-cancel" title="Cancel order"
                  aria-label="Cancel order" onclick={(e) => inlineCancel(o, e)}>
                  <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                    <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
                  </svg>
                </button>
              {:else}
                <button type="button" class="oc-act-btn" title="Repeat as new order"
                  aria-label="Repeat as new order" onclick={(e) => inlineRepeat(o, e)}>
                  <svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">
                    <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                    <path d="M13.5 2v3.5H10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </button>
              {/if}
            </div>
          </div>
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

{#if _orderChartModalOpen && _entrySymbol}
  <ChartModal
    symbol={_entrySymbol}
    exchange=""
    mode="live"
    onClose={() => _orderChartModalOpen = false} />
{/if}

{#if _rowChartModalSym}
  <ChartModal
    symbol={_rowChartModalSym}
    exchange={_rowChartModalExch}
    onClose={() => { _rowChartModalSym = ''; _rowChartModalExch = ''; }} />
{/if}

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
     Hard `height` (not just min-height) is required so the inner
     flex chain (`.oc-act-body` → `.oc-act-scroll` / `.oc-book-grid`)
     resolves to a finite height. With only `min-height`, the chain
     was unbounded and `overflow-y: auto` never triggered, so the
     log tab silently overflowed off-screen. */
  .oc-page-wrap {
    height: calc(100vh - 6.5rem);
    min-height: 28rem;
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
  /* bucket-card-book retired (Order Book merged into Activity card). */

  .bucket-header { margin-bottom: 0.35rem; }
  .mp-section-label {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(251, 191, 36, 0.7);
  }
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
    border: 1px solid rgba(251, 191, 36, 0.55);
    padding: 0.22rem 0.55rem;
    border-radius: 4px;
    box-shadow: 0 1px 4px rgba(251, 191, 36, 0.18);
  }

  /* Activity-card tabs now use the same `.oc-tab` class as the
     Entry-card tabs above — single visual vocabulary for both
     strips on the page. Only the badge style remains here. */
  .oc-act-badge {
    display: inline-flex;
    align-items: center;
    padding: 0 0.32rem;
    margin-left: 0.3rem;
    border-radius: 8px;
    background: rgba(251, 191, 36, 0.18);
    border: 1px solid rgba(251, 191, 36, 0.45);
    color: #fbbf24;
    font-size: 0.5rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
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
    color: #fde68a;
    font-weight: 700;
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
  .oc-actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.5rem;
    padding: 0.4rem 0.5rem;
    background: rgba(15, 23, 42, 0.40);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 4px;
  }
  .oc-actions-spacer { flex: 1 1 0; }
  .oc-actions-mode {
    display: inline-flex;
    gap: 0.15rem;
    align-items: center;
  }
  .oc-mode-pill {
    padding: 0.18rem 0.45rem;
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 3px;
    color: #94a3b8;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .oc-mode-pill:hover { color: #c8d8f0; border-color: rgba(126, 151, 184, 0.55); }
  .oc-mode-pill.on {
    background: rgba(251, 191, 36, 0.18);
    border-color: rgba(251, 191, 36, 0.65);
    color: #fbbf24;
  }
  .oc-act-exit,
  .oc-act-basket,
  .oc-act-side,
  .oc-act-submit {
    padding: 0.32rem 0.7rem;
    border-radius: 3px;
    font-size: 0.65rem;
    font-weight: 800;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
    cursor: pointer;
    border: 1px solid;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .oc-act-exit {
    background: rgba(126, 151, 184, 0.10);
    border-color: rgba(126, 151, 184, 0.40);
    color: #94a3b8;
  }
  .oc-act-exit:hover {
    background: rgba(126, 151, 184, 0.20);
    color: #c8d8f0;
  }
  .oc-act-basket {
    background: rgba(125, 211, 252, 0.12);
    border-color: rgba(125, 211, 252, 0.45);
    color: #7dd3fc;
  }
  .oc-act-basket:hover { background: rgba(125, 211, 252, 0.22); color: #bae6fd; }
  .oc-act-side {
    background: rgba(251, 191, 36, 0.12);
    border-color: rgba(251, 191, 36, 0.45);
    color: #fbbf24;
    min-width: 3.5rem;
    text-align: center;
  }
  .oc-act-side:hover { background: rgba(251, 191, 36, 0.20); }
  .oc-act-submit {
    color: #f1f7ff;
    min-width: 7rem;
    text-align: center;
  }
  .oc-act-submit.oc-act-buy {
    background: rgba(74, 222, 128, 0.20);
    border-color: rgba(74, 222, 128, 0.65);
    color: #4ade80;
  }
  .oc-act-submit.oc-act-buy:hover { background: rgba(74, 222, 128, 0.30); }
  .oc-act-submit.oc-act-sell {
    background: rgba(248, 113, 113, 0.20);
    border-color: rgba(248, 113, 113, 0.65);
    color: #f87171;
  }
  .oc-act-submit.oc-act-sell:hover { background: rgba(248, 113, 113, 0.30); }

  /* Chart icon button next to the symbol picker in the Order Entry
     header. Same cyan-400 palette + 1.4rem sizing as the card-control
     trio (CollapseButton, FullscreenButton). */
  .oc-chart-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(34, 211, 238, 0.40);
    border-radius: 3px;
    color: #22d3ee;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    padding: 0;
  }
  .oc-chart-btn:hover:not(:disabled) {
    background: rgba(103, 232, 249, 0.18);
    color: #67e8f9;
    border-color: rgba(103, 232, 249, 0.65);
  }
  .oc-chart-btn:disabled {
    opacity: 0.38;
    cursor: not-allowed;
  }

  /* Flat section header — section label + symbol + account + tabs.
     Single horizontal line, gap between siblings. */
  .oc-entry-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin-bottom: 0.4rem;
  }

  /* Account picker chrome — when multiple brokers loaded use a Select,
     when only one the chip below renders the read-only code. */
  .oc-entry-account { min-width: 9rem; max-width: 13rem; }
  .oc-entry-acct-chip {
    display: inline-flex;
    align-items: center;
    padding: 0.18rem 0.5rem;
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(34, 211, 238, 0.35);
    border-radius: 3px;
    color: #67e8f9;
    font-size: 0.65rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
  }

  /* Tab strip lifted into the bucket-header. Compact horizontal
     scroller; each pill carries the same colour dot SymbolPanel
     used internally so the operator's mental model carries
     ("CHART = sky", "TICKET = amber", "CHAIN = green", "COMMAND =
     sky-cyan"). */
  .oc-tabs {
    display: inline-flex;
    align-items: center;
    gap: 0;
  }
  .oc-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    /* Operator: "make chain order ticket text in tabs smaller size
       and make them consistent with other text. having them in
       separate line already made the tabs prominent." Back to the
       compact section-label scale (0.6rem / weight 700) — matches
       .mp-section-label and the LogPanel tab-row sizing. The
       dedicated tab-row line is what attracts attention now. */
    padding: 0.3rem 0.7rem;
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    color: #94a3b8;
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-family: ui-monospace, monospace;
    cursor: pointer;
    transition: color 0.12s, background 0.12s, border-color 0.12s,
                box-shadow 0.12s;
  }
  .oc-tab:hover:not(.oc-tab-disabled) {
    color: #c8d8f0;
    background: rgba(255, 255, 255, 0.04);
  }
  /* Active-state glow — keep the subtle halo + slight weight bump
     so the active tab still reads clearly, but no font-size bump
     (would re-pump the strip's size). */
  .oc-tab[aria-selected="true"] {
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.30);
    font-weight: 800;
  }
  .oc-tab-disabled {
    cursor: not-allowed;
  }
  /* Tab dots dropped — none of the other tab strips on the platform
     use them (Dashboard Cap|Eq, AgentWorkspaceTabs, /admin/tokens,
     /admin/execution, /pulse). The colored bottom-border on the
     active tab already carries the identity colour. */

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
    color: #c8d8f0;
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
        rgba(74, 222, 128, 0.06) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(74, 222, 128, 0.60);
  }
  .oc-filter-card[data-status="error"] {
    background:
      linear-gradient(180deg,
        rgba(248, 113, 113, 0.18) 0%,
        rgba(248, 113, 113, 0.06) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(248, 113, 113, 0.55);
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
    color: #c8d8f0;
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
    color: #7e97b8;
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
    background: rgba(34, 211, 238, 0.08);
    border: 1px solid rgba(34, 211, 238, 0.30);
    border-radius: 8px;
    color: #7e97b8;
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
    color: #c8d8f0;
    border-color: rgba(34, 211, 238, 0.55);
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
     sky-blue (#c8d8f0) as the surrounding text; underline is the
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
    background: rgba(34, 211, 238, 0.14);
    border: 1px solid rgba(34, 211, 238, 0.55);
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
    border-color: rgba(248, 113, 113, 0.55);
    color: #f87171;
  }
  .oc-act-cancel:hover {
    background: rgba(248, 113, 113, 0.26);
    border-color: rgba(248, 113, 113, 0.85);
    color: #fca5a5;
  }

  /* Slippage chip on COMPLETE rows — green when negative (paid less),
     red when positive (paid more). Hugs the same shape as the rest
     of the .log-chip family. */
  .log-chip-slip.slip-up   { color: #fca5a5; }
  .log-chip-slip.slip-down { color: #86efac; }

  /* Tag colour coding — algo vs manual. Operator can tell at a glance
     which orders came from the ticket vs which were agent-fired. */
  .tag-manual { color: #67e8f9; background: rgba(34, 211, 238, 0.10); }
  .tag-agent  { color: #fbbf24; background: rgba(251, 191, 36, 0.10); }
</style>
