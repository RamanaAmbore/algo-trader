<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { nowStamp, logTimeIst, formatDualTz } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import LogPanel from '$lib/LogPanel.svelte';
  import BellIcon from '$lib/icons/BellIcon.svelte';
  import { fetchOrders } from '$lib/api';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import ChaseCard from '$lib/order/ChaseCard.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import { loadInstruments } from '$lib/data/instruments';
  import {
    loadAccounts,
    resolveSymbol, resolveAccount,
    setRecentSymbol, setRecentAccount,
  } from '$lib/data/accounts';
  import { createPerformanceSocket } from '$lib/ws';
  import ChartModal from '$lib/ChartModal.svelte';

  let orders        = $state([]);
  let loading       = $state(true);
  let error         = $state('');

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
  // Status counter strip below uses `orders` to compute per-status
  // counts; the Activity LogPanel handles its own data + filters.

  // OrderTicket props — opens a SymbolPanel modal pre-filled from a
  // LogPanel `lp:modify-order` event (row pencil click). The top-of-page
  // inline shell handles fresh placement; this separate modal handles
  // single-target modify / repeat.
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
      lotSize:   1,
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

<!-- Status counter strip — at-a-glance counts (All / Open / Filled /
     Rejected / Cancelled). Click any card to uncollapse the Activity
     card below; status FILTERING lives inside LogPanel's mode + account
     chips (Activity now uses the canonical 6-tab LogPanel, same as the
     modal). The cards stay un-toggled — they're informational + a
     quick "expand activity" affordance, not a filter selector. -->
<div class="grid grid-cols-5 gap-2 mt-1 mb-2">
  {#each [
    { id: 'all',       label: 'All',       count: orders.length, accent: 'inactive' },
    { id: 'open',      label: 'Open',      count: orders.filter(o => o.status === 'OPEN' || o.status === 'TRIGGER PENDING').length, accent: 'running' },
    { id: 'complete',  label: 'Filled',    count: orders.filter(o => o.status === 'COMPLETE').length,  accent: 'active' },
    { id: 'rejected',  label: 'Rejected',  count: orders.filter(o => o.status === 'REJECTED').length,  accent: 'error' },
    { id: 'cancelled', label: 'Cancelled', count: orders.filter(o => o.status === 'CANCELLED').length, accent: 'cancelled' },
  ] as f}
    <button type="button"
      onclick={() => { _colActivity = false; }}
      class="oc-filter-card"
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
  <div class="bucket-header">
    <span class="mp-section-label oc-act-title">
      <BellIcon width="12" height="12" class="oc-act-title-icon" />
      Activity
    </span>
    <span class="oc-spacer"></span>
    {#if _fsActivity}
      <RefreshButton onClick={loadOrders} loading={loading} label="activity" />
    {/if}
    <CollapseButton bind:isCollapsed={_colActivity} label="Activity" />
    <DefaultSizeButton bind:isFullscreen={_fsActivity} bind:isCollapsed={_colActivity} label="Activity" />
    <FullscreenButton bind:isFullscreen={_fsActivity} label="Activity" />
  </div>
  <div class="card-body oc-act-body" hidden={_colActivity}>
    <LogPanel defaultTab="order" pollMs={3000} />
  </div>
</section>

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

  /* Flex-fill the wrap so the Entry + Status + Chases + Activity
     stack fills algo-content vertically with no empty gap above
     the sticky footer. Mirrors the modal's behaviour: the Activity
     card has a fixed frame and the LogPanel inside it owns its own
     scroll. Page doesn't grow past the viewport; operator scrolls
     INSIDE the Activity card to see more log/order rows. Footer
     stays pinned at viewport bottom on every render. */
  .oc-page-wrap {
    flex: 1 1 0;
    min-height: 0;
  }
  .oc-fill {
    /* Activity card flexes to consume spare vertical space inside
       the wrap; LogPanel inside it absorbs overflow via its own
       flex-1 min-h-0 internal scroll. */
    flex: 1 1 0;
    min-height: 12rem;
  }
  .oc-act-body {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  /* Activity card title — bell icon + label match the ActivityLogModal's
     header so heading reads identically on both surfaces. */
  .oc-act-title { display: inline-flex; align-items: center; gap: 0.35rem; }
  :global(.oc-act-title-icon) { color: #fbbf24; flex-shrink: 0; transform: translateY(-0.5px); }

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

  /* UnifiedLog-specific row overrides retired with the custom
     book/log tabs — LogPanel renders its own card styles inside the
     Activity card now. */

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
