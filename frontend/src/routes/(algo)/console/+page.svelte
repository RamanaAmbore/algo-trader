<script>
  // Terminal page — hosts the same 3-tab OrderEntryShell as the order
  // modal but inline, so the operator can place orders by command,
  // ticket, or basket from a dedicated workspace. LogPanel below shows
  // command history + agent / order / system streams.

  import { onMount, onDestroy } from 'svelte';
  import { authStore, clientTimestamp, visibleInterval } from '$lib/stores';
  import LogPanel        from '$lib/LogPanel.svelte';
  import OrderEntryShell from '$lib/order/OrderEntryShell.svelte';

  let logLines = $state(/** @type {any[]} */ ([]));
  let logTab   = $state('terminal');
  let logTeardown;

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

  function loadCurrentLog() {
    // 'order' tab is now self-fetching via UnifiedLog in LogPanel.
    if (logTab === 'system') loadSystemLog();
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

  <!-- 3-tab order-entry shell rendered inline (no modal chrome). -->
  <div class="terminal-shell-wrap">
    <OrderEntryShell
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
