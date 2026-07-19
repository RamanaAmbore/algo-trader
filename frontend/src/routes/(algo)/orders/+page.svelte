<script>
  import { onMount, onDestroy, getContext, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { nowStamp, lastRefreshAt, logTimeIst, formatDualTz, selectedStrategyId, strategyOpenSymbols } from '$lib/stores';
  import StrategyPicker from '$lib/StrategyPicker.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import { fetchOrders } from '$lib/api';
  import { bookChanged } from '$lib/data/bookChanged';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import ChaseCard from '$lib/order/ChaseCard.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import { loadInstruments, getInstrument } from '$lib/data/instruments';
  import {
    loadAccounts,
    resolveSymbol, resolveAccount,
    setRecentSymbol, setRecentAccount,
  } from '$lib/data/accounts';
  import { createPerformanceSocket } from '$lib/ws';
  import ChartModal from '$lib/ChartModal.svelte';

  let orders        = $state([]);
  let _showLiveTs   = $state(false);
  let loading       = $state(true);
  let error         = $state('');
  let _orderLoadFails = $state(0);

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

  // ── URL state sync (slice AV) ─────────────────────────────────────
  // Persist ?symbol=…&exchange=… so the operator can bookmark
  // /orders?symbol=BANKNIFTY26JUN50000CE&exchange=NFO or share it from
  // an alert. UX audit item #7. Mirrors /charts pattern. One-shot read
  // on mount; sync via $effect on change.
  if (typeof window !== 'undefined') {
    try {
      const sp = new URLSearchParams(window.location.search);
      const s = (sp.get('symbol') || '').trim();
      if (s) _entrySymbol = s;
      const x = (sp.get('exchange') || '').toUpperCase().trim();
      if (x) _entryExchange = x;
    } catch {}
  }

  $effect(() => {
    const sym  = _entrySymbol;
    const exch = _entryExchange;
    untrack(() => {
      try {
        const url = new URL(window.location.href);
        if (sym) url.searchParams.set('symbol', sym);
        else     url.searchParams.delete('symbol');
        if (exch) url.searchParams.set('exchange', exch);
        else      url.searchParams.delete('exchange');
        const next = url.pathname + (url.search ? url.search : '');
        if (next !== page.url.pathname + page.url.search) {
          goto(next, { replaceState: true, noScroll: true, keepFocus: true });
        }
      } catch {}
    });
  });
  // Default to 'chain' — basket-building option chain is the most-used
  // surface per operator. Ticket / Command are one click away.
  // Operator: "order ticket should be first tab and chain should be
  // second tab." Ticket is the operator's most-used surface.
  let _entryActiveTab = $state(/** @type {'chain'|'ticket'} */ ('ticket'));
  let _entryAccounts  = $state(/** @type {string[]} */ ([]));

  // Counter-prop dispatch — SymbolPanel's common-actions footer
  // increments these to fire submit/basket on the active tab.
  // OrderTicket inside SymbolPanel reacts via $effect. The page no
  // longer maintains its own mode / side state — SymbolPanel's
  // shared toolbar (_sharedMode, _modalSide) drives both.
  let _triggerSubmit = $state(0);
  let _triggerBasket = $state(0);

  // Mode/chase cluster state — lifted up so the bucket-header strip
  // and the SymbolPanel inside the card render the same values.
  // SymbolPanel exposes these as bindable props (commit 1b94d34f's
  // follow-up); incrementing _triggerClearBasket fires clearBasket
  // inside SymbolPanel without a callback handle.
  let _pageChase       = $state(true);
  let _pageChaseAgg    = $state(/** @type {'low'|'med'|'high'} */ ('low'));
  let _pageBasketCount = $state(0);
  let _triggerClear    = $state(0);
  // Operator: "when orders are open or chase is active, chase activity
  // container should be shown before log panel with order activity tab
  // active". Bind ChaseCard's active count + derive the open-order
  // count from the polled orders list. Hide the Chases section when
  // both are zero — no point showing an empty card when nothing's
  // moving. LogPanel defaults to the 'order' tab already, so the
  // operator lands on Order Activity by default whenever the section
  // shows.
  let _activeChases = $state(0);
  const _openOrderCount = $derived(
    orders.filter(o => o.status === 'OPEN' || o.status === 'TRIGGER PENDING').length
  );
  const _showChases = $derived(_openOrderCount > 0 || _activeChases > 0);

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

  // Account dropdown lives in the Activity card header so it's
  // visible regardless of which tab is active — same UX the
  // ActivityLogModal uses, keeping inline + modal mounts in sync.
  // Operator: "the accounts dropdown should be displayed activity
  // header. the activity card should be in sync across all pages
  // and modals."
  /** @type {string[]} */
  let _actAccountFilter = $state([]);
  /** @type {string[]} */
  let _actAvailableAccounts = $state([]);
  /** @type {'all'|'error'|'warning'|'info'} */
  let _actLevelFilter = $state('all');
  /** @type {'all'|'open'|'complete'|'rejected'|'cancelled'} */
  let _statusFilter = $state('all');

  // Activity-card tab state. Order Book (card grid) is the default —
  // matches the LogPanel Orders tab format shown in every other
  // Status counter strip below uses `orders` to compute per-status
  // counts; the Activity LogPanel handles its own data + filters.

  // OrderTicket props — opens a SymbolPanel modal pre-filled from a
  // LogPanel `lp:modify-order` event (row pencil click). The top-of-page
  // inline shell handles fresh placement; this separate modal handles
  // single-target modify / repeat.
  let orderTicketProps = $state(/** @type {any|null} */(null));
  /** @type {{ symbol: string, exchange: string, x: number, y: number, currentQty?: number } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | 'log' | 'close' | null} */ let _ctxAction = $state(null);
  /** @type {string} */ let _ctxSym  = $state('');
  /** @type {string} */ let _ctxExch = $state('');
  /** @type {number} */ let _ctxQty  = $state(0);
  let unsub;
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);

  async function loadOrders() {
    loading = true; error = '';
    try { const d = await fetchOrders(); orders = d.rows || []; _orderLoadFails = 0; }
    catch (e) { error = e.message; _orderLoadFails += 1; }
    finally { loading = false; }
  }

  // Slice 7g — strategy-scoped orders. Drives the status-chip counts
  // ABOVE the activity card (LogPanel does its own filtering for the
  // grid below via the symbolFilter prop). Null selectedStrategyId
  // = no filter (every order counted). Active strategy = only
  // orders whose tradingsymbol is in the strategy's open-lot set;
  // empty set when the strategy has no open lots → zero counts.
  const _scopedOrders = $derived.by(() => {
    if ($selectedStrategyId == null) return orders;
    const set = $strategyOpenSymbols;
    if (set.size === 0) return [];
    return orders.filter(o => set.has(
      String(o?.tradingsymbol || o?.symbol || '').toUpperCase()
    ));
  });

  // Svelte action — attaches the `lp:modify-order` listener LogPanel
  // dispatches when the operator clicks the pencil on any order row.
  // Custom event names with colons can't ride on Svelte 5 `on:` props;
  // a `use:` action with addEventListener bypasses that restriction.
  function listenModifyOrder(/** @type {HTMLElement} */ node) {
    const handler = (/** @type {Event} */ e) => {
      const detail = /** @type {CustomEvent} */ (e).detail;
      if (detail) orderTicketProps = _buildModifyProps(detail);
    };
    node.addEventListener('lp:modify-order', handler);
    return { destroy() { node.removeEventListener('lp:modify-order', handler); } };
  }

  // Shared helper — builds the orderTicketProps shape for a modify action.
  // Called from the lp:modify-order listener on the Activity card.
  function _buildModifyProps(/** @type {any} */ o) {
    return {
      symbol:    String(o.tradingsymbol || '').toUpperCase(),
      exchange:  o.exchange || 'NFO',
      side:      o.transaction_type,
      action:    'modify',
      orderId:   String(o.order_id || ''),
      qty:       Number(o.quantity) || 0,
      lotSize:   getInstrument(String(o.tradingsymbol || '').toUpperCase())?.ls ?? 1,
      orderType: o.order_type || 'LIMIT',
      price:     o.price > 0 ? o.price : undefined,
      trigger:   o.trigger_price > 0 ? o.trigger_price : undefined,
      product:   o.product,
      account:   String(o.account || ''),
      accounts:  [],
    };
  }

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

  // book_changed bus — symmetry with /pulse + /dashboard + /admin/
  // derivatives. Redundant with the existing `order_update` WS hook
  // above (which fires on every postback, not just terminal), but
  // _debouncedLoadOrders coalesces back-to-back triggers so the
  // duplication is harmless. Keeps the mental model uniform: every
  // surface listens to the same bus.
  let _ordersBookCounter = 0;
  $effect(() => {
    const n = $bookChanged;
    if (n <= _ordersBookCounter) return;
    _ordersBookCounter = n;
    _debouncedLoadOrders();
  });
</script>

<svelte:head><title>Orders | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col oc-page-wrap">
<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Order entry</h1>
  </span>
  <span class="algo-ts-group">
    <span class="algo-ts" class:algo-ts-hidden={_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Live clock — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {$nowStamp}
    </span>
    <span class="algo-ts-vsep" aria-hidden="true">|</span>
    <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Last refresh — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {formatDualTz($lastRefreshAt)}
    </span>
  </span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <!-- Slice 7g — strategy filter chip. When an active strategy is
         picked, the orders grid narrows to rows whose symbol matches
         the strategy's open-lot universe. Hidden when no strategies
         exist. -->
    <StrategyPicker label="Strategy" />
    <RefreshButton onClick={loadOrders} loading={loading} label="orders" />
    <PageHeaderActions symbol={_entrySymbol} hideOrder={true} />
  </span>
</div>

{#if error && _orderLoadFails >= 3}
  <!-- Audit fix — short banner per ops convention (<35 chars). Full
       error detail surfaces via the `title=` hover; the raw message
       can carry long stack fragments that wrap the layout. -->
  <div class="mb-1 p-1.5 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40"
       title={error}>
    Orders feed unavailable — retry shortly.
  </div>
{/if}

<!-- Status counter strip — at-a-glance counts (All / Open / Filled /
     Rejected / Cancelled). Click any card to uncollapse the Activity
     card below; status FILTERING lives inside LogPanel's mode + account
     chips (Activity now uses the canonical 6-tab LogPanel, same as the
     modal). The cards stay un-toggled — they're informational + a
     quick "expand activity" affordance, not a filter selector. -->
<div class="grid grid-cols-5 gap-2 mt-1 mb-2">
  {#each [
    { id: 'all',       label: 'All',       count: _scopedOrders.length, accent: 'inactive' },
    { id: 'open',      label: 'Open',      count: _scopedOrders.filter(o => o.status === 'OPEN' || o.status === 'TRIGGER PENDING').length, accent: 'running' },
    { id: 'complete',  label: 'Filled',    count: _scopedOrders.filter(o => o.status === 'COMPLETE').length,  accent: 'active' },
    { id: 'rejected',  label: 'Rejected',  count: _scopedOrders.filter(o => o.status === 'REJECTED').length,  accent: 'error' },
    { id: 'cancelled', label: 'Cancelled', count: _scopedOrders.filter(o => o.status === 'CANCELLED').length, accent: 'cancelled' },
  ] as f}
    <button type="button"
      onclick={() => { _colActivity = false; _statusFilter = /** @type {any} */ (f.id); }}
      class="oc-filter-card"
      class:is-active={_statusFilter === f.id}
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
  class:is-collapsed={_colEntry}
  style="--ch-padding:0.35rem 0.5rem">
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
  <CardHeader
    bind:isCollapsed={_colEntry}
    bind:isFullscreen={_fsEntry}
    label="Order Entry"
    onRefresh={loadOrders}
    bind:refreshLoading={loading}
    showSearch={false}
  >
    {#snippet left()}
      <span class="oc-entry-label">
        <svg class="oc-entry-icon" width="13" height="13" viewBox="0 0 16 16"
             fill="none" stroke="currentColor" stroke-width="1.5"
             stroke-linecap="round" aria-hidden="true">
          <rect x="3.2" y="2" width="9.6" height="12" rx="1.2" />
          <path d="M5.5 6h5M5.5 8.5h5M5.5 11h3" stroke-width="1.4" />
        </svg>
        ORDER ENTRY
      </span>
      <!-- Operator: "mode and chase should be left aligned. chase value
           should be selectable." Cluster sits left, immediately after
           the ORDER ENTRY label. The chase + aggressiveness collapse
           into a single Select dropdown so the operator picks one of
           off / low / med / high in one click. State is two-way bound
           to the SymbolPanel below so a flip in either surface updates
           the other. -->
      <span class="oc-header-cluster">
        <!-- Operator: "on cold start show chase with L as active."
             Default 'low' so L is amber-highlighted out of the gate. -->
        <span class="oes-common-chase-label on" title="Chase is active">CHASE</span>
        <div class="oes-common-chase-agg" role="group" aria-label="Chase aggressiveness">
          <button type="button" class="oes-common-chase-agg-pill"
                  class:on={_pageChaseAgg === 'low'}
                  title="Low — patient. Pegs to your own side; fills only if the market lifts it."
                  onclick={() => _pageChaseAgg = 'low'}>L</button>
          <button type="button" class="oes-common-chase-agg-pill"
                  class:on={_pageChaseAgg === 'med'}
                  title="Medium — peg to midpoint of bid+ask."
                  onclick={() => _pageChaseAgg = 'med'}>M</button>
          <button type="button" class="oes-common-chase-agg-pill"
                  class:on={_pageChaseAgg === 'high'}
                  title="High — urgent. Crosses the spread to take liquidity on the next tick."
                  onclick={() => _pageChaseAgg = 'high'}>H</button>
        </div>
        {#if _pageBasketCount > 0}
          <button type="button" class="oes-common-clear oes-common-clear-inline"
            title="Clear all basket legs"
            onclick={() => _triggerClear++}>Clear</button>
        {/if}
      </span>
    {/snippet}
  </CardHeader>
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
      triggerClearBasket={_triggerClear}
      bind:chase={_pageChase}
      bind:chaseAgg={_pageChaseAgg}
      bind:basketCount={_pageBasketCount}
      bind:activeTab={_entryActiveTab}
      defaultTab="ticket"
      symbol={_entrySymbol}
      exchange={_entryExchange}
      account={_entryAccount}
      accounts={_entryAccounts}
      action="open"
      onSymbolChange={(sym) => { _entrySymbol = sym; }}
      onSubmit={(payload) => {
        if (payload?.mode === 'draft') return;
        loadOrders();
      }}
      onClose={() => { /* inline mode — no close affordance */ }} />
    {#if isDemo}
      <div class="mt-2 text-[0.62rem] text-[var(--c-muted)] font-mono">
        Demo: read-only view
      </div>
    {/if}
  </div>
</section>

<!-- Chases in flight — every OPEN algo_orders row across paper /
     live / shadow. Per-row Kill button cancels the chase
     (paper-engine flip OR broker.cancel_order). Reusable card; see
     ChaseCard.svelte for the markup + polling. Section hides when
     no chases are running AND no orders are OPEN — keeps the page
     clean during idle hours. -->
{#if _showChases}
<section class="bucket-card bucket-card-chase mb-2">
  <CardHeader showControls={false}>
    {#snippet left()}
      <span class="mp-section-label">Chases</span>
    {/snippet}
  </CardHeader>
  <div class="card-body oc-chase-body">
    <ChaseCard pollMs={3000} onKilled={() => loadOrders()} bind:activeCount={_activeChases} />
  </div>
</section>
{:else}
<!-- Even when hidden, keep ChaseCard mounted so its poller updates
     _activeChases — without this the section would never re-appear on
     the first chase fire. compact + display:none keeps the DOM small. -->
<div style="display:none"><ChaseCard pollMs={3000} compact bind:activeCount={_activeChases} /></div>
{/if}

<!-- Order Activity card — same 6-tab LogPanel surface the ActivityLogModal
     mounts (Orders · Agents · Terminal · Ticks · System · News). The card
     header carries the modal's "Activity" wording + bell icon so heading
     text matches; LogPanel renders its own tab strip + the live mode
     chip (top-right of the tab row, mode-pill-paper/-live/-sim). Modify /
     Cancel / Reconcile actions live on every OrderCard inside LogPanel
     itself so they ship to the modal too — single source of truth for
     order-row actions.

     Listener for lp:modify-order: LogPanel dispatches this CustomEvent
     when the operator clicks the inline pencil on any row; the host
     opens its OrderTicket modal pre-filled. Same plumbing the Activity
     modal uses. -->
<section class="bucket-card bucket-card-activity oc-fill mb-2"
  class:fs-card-on={_fsActivity}
  class:is-collapsed={_colActivity}
  use:listenModifyOrder>
  <div class="card-body oc-act-body" hidden={_colActivity}>
    <!-- ActivityLogSurface with label="ACTIVITY" so LogPanel renders
         its own tab-row header (label chip, filters, card buttons).
         The external CardHeader is removed — LogPanel owns its chrome. -->
    <ActivityLogSurface
      defaultTab="order"
      context="card-wide"
      label="ACTIVITY"
      cardId="orders-activity"
      onRefresh={loadOrders}
      bind:isCollapsed={_colActivity}
      bind:isFullscreen={_fsActivity}
      statusFilter={_statusFilter}
      symbolFilter={$selectedStrategyId == null ? null : $strategyOpenSymbols}
      bind:accountFilter={_actAccountFilter}
      bind:availableAccounts={_actAvailableAccounts}
      bind:levelFilter={_actLevelFilter} />
  </div>
</section>

</div>

{#if orderTicketProps}
  <SymbolPanel
    defaultTab='ticket'
    symbol={orderTicketProps?.symbol}
    exchange={orderTicketProps?.exchange}
    side={orderTicketProps?.side}
    action={orderTicketProps?.action}
    orderId={orderTicketProps?.orderId}
    qty={orderTicketProps?.qty}
    lotSize={orderTicketProps?.lotSize}
    orderType={orderTicketProps?.orderType}
    price={orderTicketProps?.price}
    trigger={orderTicketProps?.trigger}
    product={orderTicketProps?.product}
    accounts={orderTicketProps?.accounts}
    account={orderTicketProps?.account}
    currentQty={orderTicketProps?.currentQty ?? 0}
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
    symbol={_ctxMenu?.symbol}
    exchange={_ctxMenu?.exchange}
    x={_ctxMenu?.x}
    y={_ctxMenu?.y}
    currentQty={_ctxMenu?.currentQty ?? 0}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      _ctxQty  = _ctxMenu?.currentQty ?? 0;
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

<!-- Close-position flow — slice AV audit fix. Right-click on a
     row with non-zero qty → "Close (sell)" / "Close (buy)" menu
     item → opens SymbolPanel in close-action mode with the held
     qty pre-seeded. Operator-saved clicks: 3 → 1. -->
{#if _ctxAction === 'close'}
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    action="close"
    currentQty={_ctxQty}
    onSubmit={() => {}}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'log'}
  <ActivityLogModal onClose={() => { _ctxAction = null; }} />
{/if}

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }

  /* Natural-flow wrap — Entry + Status + Chases + Activity stack at
     their content heights. On short pages the wrap fills algo-content
     via `flex: 1 1 auto` (no gap above the sticky footer). On tall
     pages the wrap grows past algo-content; algo-card + body grow with
     it; the document becomes scrollable and the operator can scroll
     down to reveal the last card. Sticky algo-footer in the layout
     stays pinned at the viewport bottom while scrolling.

     Critical not to use `min-height: 0` here — that would let the wrap
     shrink below its content and the last card would clip again. The
     default `min-height: auto` keeps the wrap at least as tall as its
     intrinsic content so every card is reachable. */
  .oc-page-wrap {
    flex: 1 1 auto;
  }
  .oc-fill {
    /* Activity card grows to consume spare vertical space when the
       wrap is taller than its other children (short Entry, no Chases).
       Floor at 12rem so it never collapses to a header strip even when
       the operator has no orders or log entries yet. */
    flex: 1 1 auto;
    min-height: 12rem;
  }
  .oc-act-body {
    display: flex;
    flex-direction: column;
    /* Cap the body so LogPanel's tab content (Orders / Agents /
       Terminal / Ticks / System / News) scrolls INTERNALLY rather
       than blowing the card to whatever the row count produces.
       Page scroll handles "more cards"; tab scroll handles "more
       rows than fit in the visible card body". 70vh keeps the card
       comfortably under the fold; 18rem floor stops it from
       collapsing on empty data. `overflow: hidden` makes the body a
       containing block so LogPanel's `flex-1 min-h-0` resolves to a
       finite height and its inner scrolls activate. */
    min-height: 18rem;
    max-height: 70vh;
    overflow: hidden;
  }
  /* Fullscreen mode pins the Activity card to the viewport; re-enable
     the inner flex chain so LogPanel's heightClass="flex-1 min-h-0"
     resolves correctly to the modal-style frame, and lift the body
     caps so the maximised card fills its modal frame. */
  :global(.bucket-card-activity.fs-card-on) .oc-act-body {
    flex: 1 1 0;
    min-height: 0;
    max-height: none;
  }
  /* Activity card title styles moved into LogPanel's .lp-label chrome. */
  :global(.oc-act-title-icon) { color: var(--c-action); flex-shrink: 0; transform: translateY(-0.5px); }
  /* .oc-act-acct CSS lifted into ActivityAccountSelect.svelte (the
     shared component). Operator: "activity should use the same
     reusable code across all the pages and modals." */

  /* Card chrome — full 1.5px white-10% box-border plus a 3px colored
     left-edge accent stripe per card type. Each card has its own
     identity colour so the operator can tell them apart at a glance
     without reading the section label:
       • Order Entry    → amber-400 (writing surface — operator action)
       • Order Activity → cyan-400  (live data stream)
       • Order Book     → green-400 (records / history)
     Industry analogue: Splunk panel side-stripe, Datadog widget
     accent, Bloomberg PRTU section identity bars. */
  /* Local .bucket-card chrome retired — inherits the canonical
     flat chrome from app.css. */
  /* Order Entry card overrides `.bucket-card`'s inner padding to
     zero — SymbolPanel's picker / tabs / body / common-actions
     each define their own horizontal inset, the same way they do
     inside the modal panel chrome. Card-body still ships a small
     inner bottom buffer so the common-actions row doesn't slam
     against the bucket-card border. */
  .bucket-card-entry { padding: 0 0 0.4rem 0; }
  .bucket-card-entry > .card-body { padding: 0; }
  /* Hide the outer chase card chrome when ChaseCard renders nothing
     (no active chases). Avoids an empty card on idle pages. */
  .bucket-card-chase:not(:has(.cc-root)) { display: none; }
  .oc-chase-body { padding: 0; }
  :global(.oc-chase-body .cc-root) { width: 100%; }
  /* bucket-card-book retired (Order Book merged into Activity card). */

  /* .mp-section-label is defined globally in app.css. */
  /* Match each card's section-label colour to its left-edge accent. */
  /* .bucket-card-activity section-label color override retired —
     section labels now share one default treatment so cards
     don't carry per-section color schemes. */
  /* Operator: "order entry card header in order page and modal should
     be more prominent". Bumps the section label to a chip-style amber
     pill so the "ORDER ENTRY" reads at a glance from across the
     screen. Activity card label scoped separately so it keeps its
     section-specific cyan. */

  /* History list — retired with the History tab. Style block kept
     empty for callsite stability but no element uses it. */

  /* UnifiedLog-specific row overrides retired with the custom
     book/log tabs — LogPanel renders its own card styles inside the
     Activity card now. */

  /* Order History tab dead CSS removed (~50 lines): oc-act-empty,
     oc-act-head[-done], oc-act-count, oc-act-row[-done],
     oc-act-status[-pending|-complete|-rejected|-cancelled],
     oc-act-side, oc-act-qty, oc-act-sym, oc-act-px, oc-act-meta. */

  /* Common action footer at the bottom of the Order Entry card.
     Mode pills · Exit · +Basket · Side · BUY/SELL submit. The
     buttons dispatch to whichever tab is active (Ticket / Chain /
     Command) so the operator always sees the same row of
     affordances regardless of which entry channel they're using. */
  /* .oc-actions / .oc-mode-pill / .oc-act-{exit,basket,side,submit}
     CSS retired — the page-level custom footer was replaced by
     SymbolPanel's shared .oes-common-actions block so both surfaces
     (modal + /orders) read from one component. ~80 lines dropped. */

  /* "ORDER ENTRY" label — matches the page's `.mp-section-label`
     convention used by Chase + Activity cards (0.6rem, weight 700,
     amber-70%, 0.08em letter-spacing). */
  .oc-entry-label {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(251, 191, 36, 0.7);
    flex-shrink: 0;
  }
  .oc-entry-icon { color: currentColor; flex-shrink: 0; width: 11px; height: 11px; }
  /* Mode + CHASE + L/M/H + Clear cluster inside the bucket header.
     Sits between the "ORDER ENTRY" label and the CardHeader middle
     spacer zone. Inline-flex so all children line up on the row's
     baseline; matches the modal's `.oes-header-cluster` cadence. */
  .oc-header-cluster {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    flex-shrink: 0;
  }
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
    font-family: var(--font-numeric);
    cursor: pointer;
    transition: border-color 0.12s, transform 0.12s, box-shadow 0.12s;
  }
  .oc-filter-card:hover {
    border-color: rgba(255, 255, 255, 0.30);
    transform: translateY(-1px);
  }
  .oc-filter-card.is-active {
    box-shadow:
      0 0 0 2px rgba(251, 191, 36, 0.55) inset,
      0 1px 0 rgba(255, 255, 255, 0.06) inset,
      0 2px 4px rgba(0, 0, 0, 0.25);
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

  /* `.oc-filter-card-on` retired — strip is informational + click-to-
     uncollapse-activity only; no toggle state. */

  /* Count number — bigger + colour-coded by status. */
  .oc-filter-count {
    font-weight: 800;
    font-size: 1.3rem;
    line-height: 1;
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
  }
  .oc-filter-card[data-status="running"]   .oc-filter-count { color: var(--c-action); }
  .oc-filter-card[data-status="active"]    .oc-filter-count { color: var(--c-long); }
  .oc-filter-card[data-status="error"]     .oc-filter-count { color: var(--c-short); }
  .oc-filter-card[data-status="cancelled"] .oc-filter-count { color: #fb923c; }

  .oc-filter-label {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-muted);
  }

  /* oc-count retired with the standalone Order Book card header. */


  /* Exchange filter strip + per-row Modify/Cancel/Reconcile buttons
     retired — the Activity card now mounts LogPanel which carries its
     own filter chips + action buttons via `.lp-oc-actions`. */

  /* Symbol text as a clickable affordance — underline on hover so
     the operator knows it's interactive. */
  :global(.oc-sym-btn) {
    cursor: pointer;
    border-radius: 2px;
    transition: color 0.1s, background 0.1s;
  }
  :global(.oc-sym-btn:hover) {
    color: #7dd3fc !important;
    text-decoration: underline;
  }

</style>
