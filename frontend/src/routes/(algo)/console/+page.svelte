<script>
  import { onMount, onDestroy } from 'svelte';
  import { authStore, clientTimestamp, visibleInterval } from '$lib/stores';
  import LogPanel        from '$lib/LogPanel.svelte';
  import CommandLineTab  from '$lib/order/CommandLineTab.svelte';
  import OrderEntryShell from '$lib/order/OrderEntryShell.svelte';

  let logLines     = $state([]);
  let agentLog     = $state([]);
  let orderLog     = $state([]);
  let logTab       = $state('terminal');
  let logTeardown;

  // Reference to CommandLineTab so we can read its cmdHistory.
  /** @type {any} */
  let cmdTabRef = $state(null);

  // When the Command tab parses an order command, it fires onParsedOrder.
  // Open the OrderEntryShell in Ticket mode pre-filled.
  /** @type {any|null} */
  let shellProps = $state(null);

  function authHeaders() {
    const token = $authStore.token;
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  async function loadSystemLog(n = 200) {
    try {
      const res = await fetch(`/api/admin/logs?n=${n}`, { headers: authHeaders() });
      const d = await res.json().catch(() => ({}));
      if (res.ok) logLines = d.lines || [];
    } catch (_) { /* ignore */ }
  }

  async function loadAgentLog() {
    try {
      const res = await fetch('/api/agents/events/recent?n=100', { headers: authHeaders() });
      agentLog = await res.json().catch(() => []);
    } catch (_) { /* ignore */ }
  }

  async function loadOrderLog() {
    try {
      const res = await fetch('/api/agents/events/recent?n=100', { headers: authHeaders() });
      orderLog = await res.json().catch(() => []);
    } catch (_) { /* ignore */ }
  }

  function loadCurrentLog() {
    if (logTab === 'system') loadSystemLog();
    else if (logTab === 'agent') loadAgentLog();
    else if (logTab === 'order') loadOrderLog();
  }

  onMount(() => {
    loadCurrentLog();
    logTeardown = visibleInterval(loadCurrentLog, 30000);
  });

  onDestroy(() => { logTeardown?.(); });
</script>

<svelte:head><title>Terminal | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col h-[calc(100vh-8rem)]">
  <div class="page-header">
    <h1 class="page-title-chip">Terminal</h1>
    <span class="algo-ts">{clientTimestamp()}</span>
  </div>

  <!-- Command input — uses the shared CommandLineTab component.
       onParsedOrder is fired when the operator types a BUY/SELL command;
       we open the OrderEntryShell in Ticket mode pre-filled. -->
  <CommandLineTab
    bind:this={cmdTabRef}
    standalone={true}
    onParsedOrder={(props) => {
      shellProps = { ...props, defaultTab: 'ticket' };
    }} />

  <!-- Log Tabs fill remaining space -->
  <div class="flex flex-col flex-1 min-h-0 mt-2">
    <LogPanel
      heightClass="flex-1 min-h-0"
      initialTab={logTab}
      cmdHistory={cmdTabRef?.cmdHistory ?? []}
      orderLog={orderLog}
      {agentLog}
      systemLog={logLines}
      onTabChange={(id) => { logTab = id; loadCurrentLog(); }}
    />
  </div>
</div>

{#if shellProps}
  <OrderEntryShell
    defaultTab={shellProps.defaultTab ?? 'ticket'}
    symbol={shellProps.symbol}
    exchange={shellProps.exchange}
    side={shellProps.side}
    action={shellProps.action}
    qty={shellProps.qty}
    lotSize={shellProps.lotSize}
    orderType={shellProps.orderType}
    price={shellProps.price}
    product={shellProps.product}
    accounts={shellProps.accounts ?? []}
    account={shellProps.account ?? ''}
    defaultMode={shellProps.defaultMode ?? 'live'}
    availableModes={shellProps.availableModes ?? ['live']}
    onSubmit={(payload) => {
      if (payload?.mode === 'draft') return;
      // PAPER / LIVE: log a confirmation alongside the command echo.
      const verb = payload?.side || '?';
      const sym  = payload?.symbol || shellProps.symbol;
      const qty  = payload?.quantity || shellProps.qty;
      // cmdTabRef.addResult is not exported; the history already has
      // the parsed-order echo. No further action needed here.
    }}
    onClose={() => shellProps = null}
  />
{/if}
