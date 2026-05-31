<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { nowStamp, logTimeIst } from '$lib/stores';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import AgentNotifications from '$lib/AgentNotifications.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import { fetchOrders, cancelOrder } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';
  import OrderDetail from '$lib/OrderDetail.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import { loadInstruments, searchByPrefix } from '$lib/data/instruments';
  import { priceFmt, qtyFmt } from '$lib/format';
  import { loadAccounts } from '$lib/data/accounts';
  import Select from '$lib/Select.svelte';
  import { createPerformanceSocket } from '$lib/ws';

  // Tab strip metadata — duplicated from SymbolPanel so /orders can
  // render the strip itself in the bucket-header (Phase A of the
  // orders-page redesign). Keep these in sync.
  const TABS = /** @type {const} */ ([
    { id: 'chart',   label: 'Chart',        dot: '#f0d070', activeTxt: '#f0d070', activeBorder: '#f0d070', activeBg: 'rgba(240,208,112,0.14)' },
    { id: 'ticket',  label: 'Order ticket', dot: '#fbbf24', activeTxt: '#fbbf24', activeBorder: '#fbbf24', activeBg: 'rgba(251,191,36,0.14)' },
    { id: 'chain',   label: 'Chain',        dot: '#4ade80', activeTxt: '#4ade80', activeBorder: '#4ade80', activeBg: 'rgba(74,222,128,0.14)' },
    { id: 'command', label: 'Command line', dot: '#7dd3fc', activeTxt: '#7dd3fc', activeBorder: '#7dd3fc', activeBg: 'rgba(125,211,252,0.14)' },
  ]);

  let orders        = $state([]);
  let loading       = $state(true);
  let error         = $state('');
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
  let _symQuery        = $state('');
  let _symOpen         = $state(false);
  let _symSuggestions  = $state(/** @type {any[]} */ ([]));
  let _symDebounce;
  async function _onSymInput(/** @type {string} */ v) {
    _symQuery = v;
    _symOpen = true;
    // Show suggestions as soon as the operator types the first char —
    // earlier debounce of 150ms made the dropdown feel laggy and never
    // appeared on fast typists who'd cleared the field before the
    // debounce fired. Awaiting loadInstruments ensures the IDB-backed
    // cache is hydrated before searchByPrefix iterates the map.
    if (_symDebounce) clearTimeout(_symDebounce);
    _symDebounce = setTimeout(async () => {
      try {
        if (!v) { _symSuggestions = []; return; }
        await loadInstruments();
        _symSuggestions = await searchByPrefix(v, 16);
      } catch (_) { _symSuggestions = []; }
    }, 60);
  }
  function _pickEntrySymbol(/** @type {any} */ inst) {
    _entrySymbol = String(inst?.sym || inst?.tradingsymbol || _symQuery).toUpperCase();
    _symQuery = '';
    _symOpen = false;
    _symSuggestions = [];
  }

  // Page-level shared state for the Order Entry shell.
  let _entryAccount = $state('');
  let _entryActiveTab = $state(/** @type {'chart'|'command'|'ticket'|'chain'} */ ('command'));
  let _entryAccounts  = $state(/** @type {string[]} */ ([]));

  // Per-card collapse + fullscreen state. No persistence (no cardId
  // on CollapseButton) so every page load opens all cards expanded
  // — matches the dashboard pattern.
  let _colEntry    = $state(false);
  let _fsEntry     = $state(false);
  let _colActivity = $state(false);
  let _fsActivity  = $state(false);
  let _colBook     = $state(false);
  let _fsBook      = $state(false);

  // Activity-card tab state. The card splits Order Log (UnifiedLog
  // of recent order/agent events) from Order History (pending +
  // completed orders for the operator's own placements).
  let _activityTab = $state(/** @type {'log'|'history'} */ ('log'));

  // Derived pending / completed lists for the Order History tab. Uses
  // the page's existing `orders` snapshot — no separate fetch.
  const _PENDING_STATUSES = new Set(['OPEN', 'TRIGGER PENDING', 'VALIDATION PENDING', 'PENDING']);
  const _historyPending = $derived(orders.filter(o => _PENDING_STATUSES.has(o.status)));
  const _historyDone    = $derived(orders.filter(o => !_PENDING_STATUSES.has(o.status)));

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
      onclick={() => filterStatus = f.id}
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
    <div class="oc-sym-pick">
      <input
        type="text"
        class="oc-sym-input"
        value={_symOpen ? _symQuery : (_entrySymbol || '')}
        placeholder="Symbol…"
        spellcheck="false"
        autocomplete="off"
        oninput={(e) => _onSymInput(/** @type {HTMLInputElement} */ (e.currentTarget).value)}
        onfocus={(e) => { _symQuery = ''; _symOpen = true; _onSymInput(/** @type {HTMLInputElement} */ (e.currentTarget).value); }}
        onblur={() => setTimeout(() => { _symOpen = false; }, 150)}
        onkeydown={(e) => {
          if (e.key === 'Enter' && _symSuggestions.length) { e.preventDefault(); _pickEntrySymbol(_symSuggestions[0]); }
          else if (e.key === 'Escape') { _symOpen = false; }
        }} />
      {#if _symOpen && _symSuggestions.length}
        <div class="oc-sym-drop">
          {#each _symSuggestions as inst (inst.sym)}
            <button type="button" class="oc-sym-row"
              onmousedown={(e) => { e.preventDefault(); _pickEntrySymbol(inst); }}>
              <span class="oc-sym-row-sym">{inst.sym}</span>
              <span class="oc-sym-row-meta">{inst.e}{inst.t ? ' · ' + inst.t : ''}</span>
            </button>
          {/each}
        </div>
      {/if}
    </div>
    <!-- Page-level Account picker — every shell tab (Chart, Ticket,
         Chain, Command) inherits this. Each tab can still override
         locally (the Ticket form's Account select stays for per-
         ticket flips; Command Line accepts an account token to
         override for that one command). -->
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
    <!-- 4-tab inline shell (Command Line default · Chart · Ticket ·
         Chain). `headerless={true}` suppresses the shell's own
         symbol picker — the bucket-header above carries it. The
         `onSymbolChange` callback is unused here (the chain tab
         doesn't surface a way to re-pick from inside the shell)
         but reserved for future tab-internal picks. -->
    <SymbolPanel
      inline
      headerless
      tabsExternal
      hideBottomPanel
      bind:activeTab={_entryActiveTab}
      defaultTab="command"
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
  </div>
</section>

<!-- Order Activity card — Order Log (UnifiedLog of agent / order
     events) + Order History (pending + completed orders from the
     page's existing snapshot). Was previously the SymbolPanel's
     internal bottom panel; lifted out into its own bucket-card so
     the operator can collapse / fullscreen it independently. -->
<section class="bucket-card bucket-card-activity mb-2"
  class:fs-card-on={_fsActivity}
  class:is-collapsed={_colActivity}>
  <div class="bucket-header">
    <span class="mp-section-label">Order Activity</span>
    <div class="oc-act-tabs" role="tablist">
      <button type="button" role="tab" class="oc-act-tab"
        class:on={_activityTab === 'log'}
        aria-selected={_activityTab === 'log'}
        onclick={() => _activityTab = 'log'}>Order Log</button>
      <button type="button" role="tab" class="oc-act-tab"
        class:on={_activityTab === 'history'}
        aria-selected={_activityTab === 'history'}
        onclick={() => _activityTab = 'history'}>
        Order History
        {#if _historyPending.length > 0}
          <span class="oc-act-badge">{_historyPending.length}</span>
        {/if}
      </button>
    </div>
    <span class="oc-spacer"></span>
    {#if _fsActivity}
      <RefreshButton onClick={loadOrders} loading={loading} label="activity" />
    {/if}
    <CollapseButton bind:isCollapsed={_colActivity} label="Order Activity" />
    <DefaultSizeButton bind:isFullscreen={_fsActivity} bind:isCollapsed={_colActivity} label="Order Activity" />
    <FullscreenButton bind:isFullscreen={_fsActivity} label="Order Activity" />
  </div>
  <div class="card-body" hidden={_colActivity}>
    {#if _activityTab === 'log'}
      <!-- Wrap in .oc-activity-log so the :global overrides above
           restyle UnifiedLog's card-mode rows to match /market's
           news-row look (flat row, time on the left, thin bottom
           divider, no left accent). -->
      <div class="oc-activity-log">
        <UnifiedLog
          filter={{}}
          pollMs={3000}
          maxRows={30}
          heightClass="oc-act-scroll"
          cardMode={true} />
      </div>
    {:else}
      <div class="oc-act-history">
        {#if _historyPending.length === 0 && _historyDone.length === 0}
          <div class="oc-act-empty">No orders yet.</div>
        {:else}
          {#if _historyPending.length > 0}
            <header class="oc-act-head">PENDING <span class="oc-act-count">{_historyPending.length}</span></header>
            {#each _historyPending as o (o.order_id)}
              <article class="oc-act-row">
                <span class="oc-act-status oc-act-status-pending">{o.status}</span>
                <span class="oc-act-side" style="{txnColor(o.transaction_type)}">{o.transaction_type}</span>
                <span class="oc-act-qty">{qtyFmt(o.quantity)}</span>
                <span class="oc-act-sym">{o.tradingsymbol}</span>
                <span class="oc-act-px">{priceFmt(o.price ?? 0)}</span>
                <span class="oc-act-meta">acct={o.account} · #{o.order_id} · {o.order_timestamp ? logTimeIst(o.order_timestamp) : ''}</span>
              </article>
            {/each}
          {/if}
          {#if _historyDone.length > 0}
            <header class="oc-act-head oc-act-head-done">COMPLETED <span class="oc-act-count">{_historyDone.length}</span></header>
            {#each _historyDone as o (o.order_id)}
              <article class="oc-act-row oc-act-row-done">
                <span class="oc-act-status oc-act-status-{o.status?.toLowerCase()}">{o.status}</span>
                <span class="oc-act-side" style="{txnColor(o.transaction_type)}">{o.transaction_type}</span>
                <span class="oc-act-qty">{qtyFmt(o.quantity)}</span>
                <span class="oc-act-sym">{o.tradingsymbol}</span>
                <span class="oc-act-px">{priceFmt(o.average_price ?? o.price ?? 0)}</span>
                <span class="oc-act-meta">acct={o.account} · #{o.order_id} · {o.order_timestamp ? logTimeIst(o.order_timestamp) : ''}</span>
              </article>
            {/each}
          {/if}
        {/if}
      </div>
    {/if}
  </div>
</section>

<!-- Order Book card — bucket-card chrome re-added per operator
     request. Has its own [Collapse · DefaultSize · Fullscreen] trio
     on the right. Filter chips + Account picker stay in the header. -->
<section class="bucket-card bucket-card-book mb-2"
  class:fs-card-on={_fsBook}
  class:is-collapsed={_colBook}>
  <div class="bucket-header">
    <span class="mp-section-label">Order Book</span>
    <span class="oc-count">
      {#if _filteredOrders.length !== orders.length}
        {_filteredOrders.length} of {orders.length}
      {:else}
        {orders.length} today
      {/if}
    </span>
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
    <span class="oc-spacer"></span>
    {#if _fsBook}
      <RefreshButton onClick={loadOrders} loading={loading} label="orders" />
    {/if}
    <CollapseButton bind:isCollapsed={_colBook} label="Order Book" />
    <DefaultSizeButton bind:isFullscreen={_fsBook} bind:isCollapsed={_colBook} label="Order Book" />
    <FullscreenButton bind:isFullscreen={_fsBook} label="Order Book" />
  </div>
  <div class="card-body" hidden={_colBook}>
    {#if loading && !orders.length}
      <div class="text-center text-muted text-xs animate-pulse py-2">Loading orders…</div>
    {:else if _filteredOrders.length}
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 mb-1 max-h-[min(40vh,22rem)] overflow-y-auto">
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
              {#if o.exchange}<span class="log-chip"><span class="log-chip-key">ex:</span>{o.exchange}</span>{/if}
              <span class="log-chip"><span class="log-chip-key">qty:</span>{qtyFmt(o.filled_quantity)}/{qtyFmt(o.quantity)}</span>
              <span class="log-chip"><span class="log-chip-key">type:</span>{o.order_type}</span>
              <span class="log-chip"><span class="log-chip-key">price:</span>{o.average_price != null ? priceFmt(o.average_price) : o.price != null ? priceFmt(o.price) : '—'}</span>
              {#if slippage(o) != null}<span class="log-chip log-chip-slip" class:slip-up={slippage(o) > 0} class:slip-down={slippage(o) < 0}><span class="log-chip-key">slip:</span>{slippage(o) > 0 ? '+' : ''}{priceFmt(slippage(o))}</span>{/if}
              {#if o.trigger_price}<span class="log-chip"><span class="log-chip-key">trigger:</span>{priceFmt(o.trigger_price)}</span>{/if}
              {#if o.validity}<span class="log-chip"><span class="log-chip-key">validity:</span>{o.validity}</span>{/if}
              <span class="log-chip"><span class="log-chip-key">product:</span>{o.product}</span>
              <span class="log-chip"><span class="log-chip-key">variety:</span>{o.variety}</span>
              {#if o.order_timestamp}<span class="log-chip"><span class="log-chip-key">time:</span>{logTimeIst(o.order_timestamp)}</span>{/if}
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
  .bucket-card-book     { border-left-color: rgba(74, 222, 128, 0.70); }

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
  .bucket-card-book     .mp-section-label { color: rgba(74, 222, 128, 0.85); }

  /* Activity card — Order Log / Order History tab strip. Same shape
     as the entry-card tabs above for visual consistency. */
  .oc-act-tabs { display: inline-flex; gap: 0; align-items: center; }
  .oc-act-tab {
    padding: 0.32rem 0.65rem;
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    color: #94a3b8;
    font-size: 0.58rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-family: ui-monospace, monospace;
    cursor: pointer;
    transition: color 0.12s, border-color 0.12s;
  }
  .oc-act-tab:hover { color: #c8d8f0; }
  .oc-act-tab.on {
    color: #67e8f9;
    border-bottom-color: rgba(34, 211, 238, 0.85);
    font-weight: 800;
  }
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

  /* History list — pending + completed orders in compact rows.
     Operator scans a flat list (sym · qty · price · meta) without
     the per-row cards from the Order Book grid above. Tightened
     to ~14rem max so the Activity card stays compact between the
     Order Entry header above and the Order Book grid below. */
  .oc-act-history {
    max-height: min(28vh, 14rem);
    overflow-y: auto;
    font-family: ui-monospace, monospace;
  }

  /* UnifiedLog rows inside the Activity card — restyled to look like
     the Market News rows on /performance + /market (see app.css
     .log-news-row). Drop the per-row left accent + box border, use
     a single thin bottom divider, time on the left + content
     flowing right. Operator scans the same shape across "log of
     events" surfaces regardless of which page they're on. */
  :global(.oc-activity-log .ul-list-cards) {
    gap: 0;
    padding: 0;
    background: transparent;
  }
  :global(.oc-activity-log .ul-card) {
    background: transparent;
    border: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 0;
    padding: 0.32rem 0.25rem;
    flex-direction: row;
    align-items: baseline;
    gap: 0.55rem;
  }
  :global(.oc-activity-log .ul-card:last-child) { border-bottom: 0; }
  :global(.oc-activity-log .ul-card:hover) {
    background: rgba(255, 255, 255, 0.02);
  }
  :global(.oc-activity-log .ul-card-head) {
    flex: 0 0 auto;
    gap: 0.35rem;
    font-size: 0.6rem;
  }
  :global(.oc-activity-log .ul-card-time) {
    color: #fde047;
    font-weight: 700;
    font-size: 0.6rem;
    letter-spacing: 0.02em;
    margin-left: 0;
    order: -1;
  }
  :global(.oc-activity-log .ul-card-msg) {
    flex: 1 1 auto;
    color: #e2e8f0;
    font-weight: 500;
    font-size: 0.66rem;
    line-height: 1.35;
    padding-left: 0;
  }
  .oc-act-empty {
    color: #7e97b8;
    font-size: 0.65rem;
    padding: 0.5rem 0.25rem;
    text-align: center;
  }
  .oc-act-head {
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #7e97b8;
    padding: 0.3rem 0.25rem 0.15rem;
    display: flex;
    gap: 0.3rem;
    align-items: center;
  }
  .oc-act-head-done { color: #4ade80; }
  .oc-act-count {
    background: rgba(126, 151, 184, 0.18);
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 8px;
    padding: 0.02rem 0.32rem;
    color: #c8d8f0;
    font-variant-numeric: tabular-nums;
  }
  .oc-act-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.25rem;
    border-bottom: 1px dashed rgba(255, 255, 255, 0.06);
    font-size: 0.62rem;
    color: #c8d8f0;
  }
  .oc-act-row-done { opacity: 0.85; }
  .oc-act-status {
    padding: 0.05rem 0.32rem;
    border-radius: 2px;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-family: ui-monospace, monospace;
  }
  .oc-act-status-pending  { background: rgba(251, 191, 36, 0.18); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.40); }
  .oc-act-status-complete { background: rgba(74, 222, 128, 0.18); color: #4ade80; border: 1px solid rgba(74, 222, 128, 0.40); }
  .oc-act-status-rejected { background: rgba(248, 113, 113, 0.18); color: #f87171; border: 1px solid rgba(248, 113, 113, 0.40); }
  .oc-act-status-cancelled { background: rgba(251, 146, 60, 0.18); color: #fb923c; border: 1px solid rgba(251, 146, 60, 0.40); }
  .oc-act-side { font-weight: 800; }
  .oc-act-qty { font-variant-numeric: tabular-nums; color: #c8d8f0; }
  .oc-act-sym { font-weight: 700; color: #f1f7ff; }
  .oc-act-px  { color: #fbbf24; font-variant-numeric: tabular-nums; }
  .oc-act-meta { color: #7e97b8; font-size: 0.55rem; margin-left: auto; }

  /* Flex spacer pushes the bucket-header's [Collapse · DefaultSize ·
     Fullscreen] trio to the card's right edge. Matches the
     `.cap-eq-spacer` pattern on /dashboard. */
  .oc-spacer { flex: 1 1 0; }

  /* Symbol picker inline in the Order Entry bucket-header — moved
     here from SymbolPanel's own `.oes-sym-pick`. Visual identity is
     identical so operators see the same control whether on /orders
     (bucket-header) or in a chain-pick modal (SymbolPanel header). */
  .oc-sym-pick {
    position: relative;
    display: inline-flex;
    align-items: center;
  }
  .oc-sym-input {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 3px;
    padding: 0.18rem 0.45rem;
    color: #fbbf24;
    font-size: 0.7rem;
    font-weight: 800;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
    width: 11rem;
    text-transform: uppercase;
  }
  .oc-sym-input:focus {
    outline: none;
    border-color: rgba(251, 191, 36, 0.55);
    background: rgba(251, 191, 36, 0.06);
  }
  .oc-sym-input::placeholder {
    color: rgba(251, 191, 36, 0.40);
    font-weight: 600;
  }
  .oc-sym-drop {
    position: absolute;
    top: 100%;
    left: 0;
    z-index: 2000;
    margin-top: 2px;
    min-width: 14rem;
    max-height: 16rem;
    overflow-y: auto;
    background: #1b2540;
    border: 1px solid rgba(251, 191, 36, 0.35);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.55);
    display: flex;
    flex-direction: column;
  }
  .oc-sym-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.2rem 0.4rem;
    background: transparent;
    border: 0;
    color: #c8d8f0;
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    cursor: pointer;
    text-align: left;
    width: 100%;
  }
  .oc-sym-row:hover {
    background: rgba(251, 191, 36, 0.12);
    color: #fbbf24;
  }
  .oc-sym-row-sym { font-weight: 700; letter-spacing: 0.03em; }
  .oc-sym-row-meta {
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
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
    padding: 0.32rem 0.65rem;
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    color: #94a3b8;
    font-size: 0.58rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-family: ui-monospace, monospace;
    cursor: pointer;
    transition: color 0.12s, background 0.12s, border-color 0.12s;
  }
  .oc-tab:hover:not(.oc-tab-disabled) {
    color: #c8d8f0;
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

  /* Inline count chip alongside the section label — at-a-glance
     "how many today" without breaking the header's single-line
     read. Same muted slate-blue tone as the Equity-tab count
     chips on /dashboard. */
  .oc-count {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.32rem;
    margin-left: 0.35rem;
    border-radius: 8px;
    background: rgba(126, 151, 184, 0.18);
    border: 1px solid rgba(126, 151, 184, 0.30);
    color: #c8d8f0;
    font-size: 0.55rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-variant-numeric: tabular-nums;
  }

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
