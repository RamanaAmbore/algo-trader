<script>
  // Terminal page — hosts the same 3-tab OrderEntryShell as the order
  // modal but inline, so the operator can place orders by command,
  // ticket, or basket from a dedicated workspace. LogPanel below shows
  // command history + agent / order / system streams.

  import { authStore, nowStamp } from '$lib/stores';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import AgentNotifications from '$lib/AgentNotifications.svelte';
  import InfoHint        from '$lib/InfoHint.svelte';
  import LogPanel        from '$lib/LogPanel.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';

  // Demo gate — anonymous-on-prod (dev anon gets redirected to /signin
  // by the algo layout before this page loads). Order writes already
  // 401 server-side via admin_guard; the banner sets expectations.
  const isDemo = $derived(!$authStore.user);

  let logTab = $state('terminal');
</script>

<svelte:head><title>Console | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col h-[calc(100vh-8rem)]">
  <div class="page-header">
    <span class="algo-title-group">
      <h1 class="page-title-chip">Console</h1>
      <InfoHint popup text="Order workspace: type commands (<code>buy ZG#### NIFTY25APRFUT 50 limit 22000</code>), use the Ticket form, or build a basket. Tokenized autocomplete drives the Command tab." />
    </span>
    <span class="algo-ts">{$nowStamp}</span>
    <span class="ml-auto"></span>
    <OrderNotifications /><AgentNotifications />
  </div>

  {#if isDemo}
    <!-- Demo banner — the command grammar + ticket form render fine
         for demo (autocomplete works), but every submit path 401s
         server-side. Sets the expectation so the operator doesn't
         hammer a dead Submit. -->
    <div class="mb-2 p-2 rounded bg-purple-500/10 border border-purple-500/30 text-[0.65rem] text-purple-200">
      <strong class="text-purple-100">Demo view.</strong>
      Browse the command grammar + autocomplete. Submit + write paths are
      disabled — see <a href="/showcase" class="underline hover:text-purple-50">the tour</a>
      for what this terminal does in production.
    </div>
  {/if}

  <!-- 3-tab order-entry shell rendered inline (no modal chrome). -->
  <div class="terminal-shell-wrap">
    <SymbolPanel
      inline
      defaultTab="command"
      symbol=""
      action="open"
      side="BUY"
      onSubmit={() => { /* successes are surfaced via the shell's own UI */ }}
      onClose={() => { /* no close affordance in inline mode */ }} />
  </div>

  <!-- Log Tabs fill remaining space -->
  <div class="flex flex-col flex-1 min-h-0 mt-2">
    <LogPanel
      heightClass="flex-1 min-h-0"
      defaultTab={logTab}
      tabs={['terminal','system','news']}
      onTabChange={(id) => { logTab = id; }}
    />
  </div>
</div>

<style>
  .terminal-shell-wrap {
    flex-shrink: 0;
    margin-bottom: 0.4rem;
  }
</style>
