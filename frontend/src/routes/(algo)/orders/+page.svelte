<script>
  import { onMount, onDestroy, getContext, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { selectedStrategyId, strategyOpenSymbols } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import StrategyPicker from '$lib/StrategyPicker.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import OrderBook from '$lib/OrderBook.svelte';
  import HeldOrdersCard from '$lib/HeldOrdersCard.svelte';
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
  import { groupPropFor } from '$lib/data/pulseDetached.svelte.js';
  import { isSharedContextAction, dispatchSharedContextAction } from '$lib/data/symbolContextDispatch.js';
  import ChartModal from '$lib/ChartModal.svelte';
  import { payoffDrafts } from '$lib/data/payoffDrafts.svelte.js';

  let orders        = $state([]);
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
  let _entryActiveTab = $state(/** @type {'chain'|'ticket'|'chart'|'cli'} */ ('ticket'));
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
  // Set of broker_order_ids that are currently managed by the chase engine.
  // Populated from ChaseCard's bindable chaseOrderIds prop so we can
  // exclude those from the pendingOrders list (they already appear as chase rows).
  let _chaseOrderIds = $state(/** @type {Set<string>} */ (new Set()));
  const _openOrderCount = $derived(
    orders.filter(o =>
      o.status === 'OPEN' ||
      o.status === 'TRIGGER PENDING' ||
      o.status === 'TRIGGER_PENDING'
    ).length
  );
  // OPEN / TRIGGER_PENDING broker orders that are NOT active chases.
  // These are plain working orders the operator can modify or cancel.
  const _pendingOrders = $derived(
    orders.filter(o =>
      (o.status === 'OPEN' || o.status === 'TRIGGER PENDING' || o.status === 'TRIGGER_PENDING') &&
      !_chaseOrderIds.has(String(o.order_id || ''))
    )
  );
  // Derive an array from the reactive payoffDrafts Map so ChaseCard
  // re-renders whenever a draft is added or removed.
  const _draftOrdersList = $derived([...payoffDrafts.value.values()]);
  const _showChases = $derived(_openOrderCount > 0 || _activeChases > 0 || _draftOrdersList.length > 0 || _pendingOrders.length > 0);

  // Per-card fullscreen state. No persistence (no cardId) so every
  // page load opens both cards expanded — matches the dashboard
  // pattern. Collapse removed app-wide (2026-10-13 header
  // button-group standardization).
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _loadOrdersTimer = null;
  function _debouncedLoadOrders() {
    if (_loadOrdersTimer) clearTimeout(_loadOrdersTimer);
    _loadOrdersTimer = setTimeout(loadOrders, 250);
  }

  let _fsActivity  = $state(false);

  // Account dropdown lives in the Activity card header so it's
  // visible regardless of which tab is active — same UX the
  // ActivityLogModal uses, keeping inline + modal mounts in sync.
  // Operator: "the accounts dropdown should be displayed activity
  // header. the activity card should be in sync across all pages
  // and modals."
  /** @type {string[]} */
  let _actAccountFilter = $state([]);
  /** @type {'chase'|'open'|'complete'|'rejected'|'cancelled'} */
  let _statusFilter = $state('open');

  // Activity-card tab state. Order Book (card grid) is the default —
  // matches the LogPanel Orders tab format shown in every other
  // Status counter strip below uses `orders` to compute per-status
  // counts; the Activity LogPanel handles its own data + filters.

  // OrderTicket props — opens a SymbolPanel modal pre-filled from a
  // LogPanel `lp:modify-order` event (row pencil click). The top-of-page
  // inline shell handles fresh placement; this separate modal handles
  // single-target modify / repeat.
  let orderTicketProps = $state(/** @type {any|null} */(null));
  /** @type {{ symbol: string, exchange: string, x: number, y: number, currentQty?: number, watchlistInfo?: { inWatchlist: boolean } | null } | null} */
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
      variety:   o.variety || 'regular',
      account:   String(o.account || ''),
      accounts:  [],
    };
  }

  // Opens the order ticket pre-filled from a draft payoff entry. Sets
  // initialDraftId so the ticket knows to operate in "edit draft" mode
  // (button label = "Update Draft", cancel removes the draft from the store).
  function _openDraftTicket(/** @type {any} */ draft) {
    const sym  = String(draft.symbol || '').toUpperCase();
    const inst = getInstrument(sym);
    const lot  = Number(inst?.ls || 1);
    const absQty = Math.abs(Number(draft.qty || 0));
    const side = (draft.transaction_type === 'SELL' || Number(draft.qty || 0) < 0)
      ? 'SELL' : 'BUY';
    orderTicketProps = {
      symbol:         sym,
      exchange:       draft.exchange || inst?.e || 'NFO',
      side,
      action:         'open',
      qty:            absQty || lot,
      lotSize:        lot,
      price:          draft.avg_cost != null ? Number(draft.avg_cost) : undefined,
      accounts:       _entryAccounts,
      account:        _entryAccount,
      // Real backend AlgoOrder id (number) — must match the Map key
      // type exactly (payoffDrafts.value.get/has use strict key
      // equality; a String()-coerced id would silently never match).
      initialDraftId: draft.id,
    };
  }

  onMount(() => {
    loadOrders();
    // Hydrate OrderTicket-originated payoff drafts from the backend
    // (2026-10) so they survive a page refresh — see payoffDrafts.svelte.js.
    payoffDrafts.load();
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
  // No Vitest test: onDestroy cleanup in .svelte components requires a component
  // mount harness; no corresponding Vitest test file exists for this route component.
  onDestroy(() => { unsub?.(); if (_loadOrdersTimer) clearTimeout(_loadOrdersTimer); });

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
    <h1 class="page-title-chip">Orders</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <!-- Slice 7g — strategy filter chip. When an active strategy is
         picked, the orders grid narrows to rows whose symbol matches
         the strategy's open-lot universe. Hidden when no strategies
         exist. -->
    <StrategyPicker label="Strategy" />
    <RefreshButton onClick={loadOrders} loading={loading} label="orders" />
    <PageHeaderActions symbol={_entrySymbol} hideOrder={true} logTabHint="order" />
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

<!-- Order Entry header row removed entirely (2026-10, operator request)
     — no title text, no card-control trio (Fullscreen/Refresh included).
     SymbolPanel below still renders `headerless` (its own internal
     header strip stays suppressed) so there is now no header row at
     all above the picker row / tab strip. This also removes the only
     manual "force refresh" and "fullscreen" triggers this card had —
     accepted tradeoff, SymbolPanel still refreshes on its own normal
     polling cadence. -->
<section class="bucket-card bucket-card-entry mt-1">
  <div class="card-body">
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
      onClose={() => { /* inline mode — no close affordance */ }}>
      {#snippet pickerSuffix()}
        {#if _pageBasketCount > 0}
          <button type="button" class="oes-common-clear oes-common-clear-inline"
            title="Clear all basket legs"
            onclick={() => _triggerClear++}>Clear</button>
        {/if}
      {/snippet}
    </SymbolPanel>
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
<section class="bucket-card bucket-card-chase">
  <!-- ChaseCard now owns its own single CardHeader (2026-10 CardHeader
       adoption sweep) — the page no longer wraps it in a second "Chases"
       bar, which used to stack two shaded header rows on top of each
       other. ChaseCard's own title ("Chases in flight") replaces this
       page's plain "Chases" label. -->
  <div class="card-body oc-chase-body">
    <ChaseCard pollMs={3000} onKilled={() => loadOrders()} bind:activeCount={_activeChases}
      bind:chaseOrderIds={_chaseOrderIds}
      draftOrders={_draftOrdersList}
      onDraftRemove={(id) => payoffDrafts.remove(id)}
      onDraftClick={_openDraftTicket}
      pendingOrders={_pendingOrders}
      onPendingModify={(o) => { orderTicketProps = _buildModifyProps(o); }} />
  </div>
</section>
{:else}
<!-- Even when hidden, keep ChaseCard mounted so its poller updates
     _activeChases — without this the section would never re-appear on
     the first chase fire. compact + display:none keeps the DOM small. -->
<div style="display:none"><ChaseCard pollMs={3000} compact bind:activeCount={_activeChases}
  bind:chaseOrderIds={_chaseOrderIds}
  draftOrders={_draftOrdersList}
  onDraftRemove={(id) => payoffDrafts.remove(id)}
  onDraftClick={_openDraftTicket}
  pendingOrders={_pendingOrders}
  onPendingModify={(o) => { orderTicketProps = _buildModifyProps(o); }} /></div>
{/if}

<!-- Order Activity card — NOT the tabbed LogPanel surface (that claim was
     stale/inaccurate; corrected 2026-10-13). `OrderBook.svelte` is a
     separate, non-tabbed order-card grid extracted from LogPanel's own
     `order` tab (see that component's own header doc) — it has no tab
     strip of its own. The REAL tabbed LogPanel surface on /orders is
     the bell-icon modal (ActivityLogModal), opened via PageHeaderActions
     (logTabHint="order" defaults it straight to the Orders tab). This
     card's header carries the same "Activity" wording + bell icon as the
     modal purely so heading text matches, with no shared LogPanel
     instance underneath. Modify / Cancel / Reconcile actions live on
     every OrderCard inside OrderBook itself, mirroring (not sharing)
     LogPanel's own order-row actions — see OrderBook.svelte's own doc
     comment for the "mirrors LogPanel" convention this relies on.

     Listener for lp:modify-order: OrderBook dispatches this CustomEvent
     when the operator clicks the inline pencil on any row; the host
     opens its OrderTicket modal pre-filled. Same plumbing the Activity
     modal's own LogPanel instance uses, kept in sync by convention. -->
<section class="bucket-card bucket-card-activity oc-fill"
  class:fs-card-on={_fsActivity}
  use:listenModifyOrder>
  <HeldOrdersCard />
  <OrderBook
    statusFilter={_statusFilter}
    accountFilter={_actAccountFilter}
    bind:isFullscreen={_fsActivity}
  />
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
    initialDraftId={orderTicketProps?.initialDraftId ?? null}
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
    showOpenInOptions
    showCopySymbol
    group={groupPropFor(_ctxMenu?.symbol)}
    watchlist={_ctxMenu?.watchlistInfo ?? null}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      if (isSharedContextAction(action)) {
        dispatchSharedContextAction(action, sym, exch);
        _ctxMenu = null;
        return;
      }
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
    currentQty={_ctxQty}
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
    side={_ctxQty > 0 ? 'SELL' : 'BUY'}
    orderType="MARKET"
    account={_entryAccount}
    accounts={_entryAccounts}
    onSubmit={() => { loadOrders(); }}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'log'}
  <ActivityLogModal onClose={() => { _ctxAction = null; }} />
{/if}

<style>
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

  /* "ORDER ENTRY" label (.oc-entry-label/.oc-entry-icon) removed with
     the header row itself (2026-10, operator request — the whole
     Order Entry CardHeader is gone, not just restyled). */
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
