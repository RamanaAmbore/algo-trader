<script>
  // Console — plain command-line terminal + the canonical 5-tab activity
  // log. The page's primary input is CommandLineTab (typed grammar
  // commands); the inline LogPanel below carries the same 5-tab surface
  // the page-header Log icon opens in a modal — operator can use either.
  //
  // A parsed command opens SymbolPanel as a modal (defaultTab='ticket')
  // pre-filled with the parsed values, so the operator can review +
  // Submit. Keeps the safety net while making plain-text the primary UI.

  import { authStore } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import CommandLineTab from '$lib/order/CommandLineTab.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';

  const isDemo = $derived(!$authStore.user);

  let logTab = $state('terminal');
  // Manual-refresh bump for the inline LogPanel — clicking the page-
  // header Refresh icon rotates the badge + asks LogPanel to re-poll
  // immediately rather than waiting for its next tick.
  let _refreshKey = $state(0);
  let _refreshing = $state(false);
  function _refresh() {
    _refreshing = true;
    _refreshKey++;
    setTimeout(() => { _refreshing = false; }, 400);
  }

  // SymbolPanel-as-modal state — opened when CommandLineTab fires a
  // parsed order. Operator confirms / edits / submits from the modal.
  let _ticketProps = $state(/** @type {any} */ (null));
  function _onParsedOrder(/** @type {any} */ props) {
    _ticketProps = { ...props, defaultTab: 'ticket' };
  }
  function _closeTicket() { _ticketProps = null; }
</script>

<svelte:head><title>Console | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col h-[calc(100vh-11rem)]">
  <div class="page-header">
    <span class="algo-title-group">
      <h1 class="page-title-chip">Console</h1>
    </span>
    <AlgoTimestamp />
    <span class="ml-auto"></span>
    <span class="page-header-actions">
      <RefreshButton onClick={_refresh} loading={_refreshing} label="console" />
      <PageHeaderActions />
    </span>
  </div>

  {#if isDemo}
    <div class="mb-2 p-2 rounded bg-purple-500/10 border border-purple-500/30 text-[0.65rem] text-purple-200">
      <strong class="text-purple-100">Demo view.</strong>
      Browse the command grammar + autocomplete. Submit + write paths are
      disabled — see <a href="/showcase" class="underline hover:text-purple-50">the tour</a>
      for what this terminal does in production.
    </div>
  {/if}

  <!-- Plain command-line entry — was the 3-tab SymbolPanel shell; reduced
       to just the grammar-driven command bar. Parsed commands fan out
       to the SymbolPanel ticket modal for confirmation. -->
  <div class="console-cmd-wrap">
    <CommandLineTab
      onParsedOrder={_onParsedOrder}
      prefillSide=""
      prefillAccount=""
      prefillSymbol=""
      prefillQty={0}
      prefillPrice={0}
      prefillOrderType="LIMIT" />
  </div>

  <!-- Activity log — same 5 tabs as the modal you'd get from the Log
       icon on every other page (Order Book · Agent Log · Terminal ·
       System · News). Terminal is the default since this page is the
       Terminal. -->
  <div class="flex flex-col flex-1 min-h-0 mt-2">
    <!-- Tab list inherited from LogPanel's default — keeps every
         surface (Activity modal, Order modal bottom panel, this
         /console mount, /automation) in sync without duplicating the
         array per callsite. -->
    <ActivityLogSurface
      context="page"
      heightClass="flex-1 min-h-0"
      defaultTab={logTab}
      hideInlineAccountFilter={false}
      onTabChange={(id) => { logTab = id; }}
    />
  </div>
</div>

{#if _ticketProps}
  <!-- Order modal carries its own Order Book + Agent Log bottom tabs as
       a self-contained surface — modal is its own world, distinct from
       the page-level LogPanel below the command bar. -->
  <SymbolPanel
    symbol={_ticketProps.symbol || ''}
    exchange={_ticketProps.exchange || ''}
    defaultTab="ticket"
    side={_ticketProps.side || 'BUY'}
    qty={_ticketProps.qty || 0}
    price={_ticketProps.price || 0}
    orderType={_ticketProps.orderType || 'LIMIT'}
    account={_ticketProps.account || ''}
    accounts={[]}
    onSubmit={_closeTicket}
    onClose={_closeTicket}
  />
{/if}

<style>
  .console-cmd-wrap {
    flex-shrink: 0;
    margin-bottom: 0.4rem;
  }
</style>
