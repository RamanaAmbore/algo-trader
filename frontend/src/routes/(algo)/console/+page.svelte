<script>
  // Terminal page — hosts the same 3-tab OrderEntryShell as the order
  // modal but inline, so the operator can place orders by command,
  // ticket, or basket from a dedicated workspace. LogPanel below shows
  // command history + agent / order / system streams.

  import { clientTimestamp } from '$lib/stores';
  import InfoHint        from '$lib/InfoHint.svelte';
  import LogPanel        from '$lib/LogPanel.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';

  let logTab = $state('terminal');
</script>

<svelte:head><title>Terminal | RamboQuant Analytics</title></svelte:head>

<div class="flex flex-col h-[calc(100vh-8rem)]">
  <div class="page-header">
    <h1 class="page-title-chip">Terminal</h1>
    <InfoHint popup text="Order workspace: type commands (<code>buy ZG#### NIFTY25APRFUT 50 limit 22000</code>), use the Ticket form, or build a basket. Tokenized autocomplete drives the Command tab." />
    <span class="algo-ts">{clientTimestamp()}</span>
  </div>

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
