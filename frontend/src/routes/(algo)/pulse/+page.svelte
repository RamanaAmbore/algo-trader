<script>
  // Thin page wrapper — the unified market-pulse component lives in
  // $lib/MarketPulse.svelte. Both /pulse and /dashboard compose it
  // with different presets so the merge engine + symbol-cell renderer
  // + format helpers stay in one place.
  import MarketPulse from '$lib/MarketPulse.svelte';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import { nowStamp } from '$lib/stores';
</script>

<svelte:head><title>Pulse | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="pulse-title-group">
    <h1 class="page-title-chip">Pulse</h1>
    <InfoHint popup text={'Live broker book — positions, holdings, watchlist quotes, movers and pinned indices in one grid. Tap any row to open the order ticket. Use <b>Show…</b> to toggle sources. Account multiselect scopes positions + holdings (watchlists stay visible). The toolbar carries an immediate-refresh button; auto-refresh cadence is driven by <span class="font-mono">pulse.tick_interval_ms</span> in /admin/settings.'} />
  </span>
  <span class="algo-ts ml-auto">{$nowStamp}</span><OrderNotifications />
</div>

<!-- accountPicker=true mounts the broker-account MultiSelect in the
     toolbar (right cluster, next to source toggles). Empty selection
     = all accounts; otherwise positions + holdings inputs to
     buildUnified are scoped to the chosen set. Watchlist + option
     underlyings stay visible. -->
<MarketPulse title="Pulse" flat={true} accountPicker={true} />

<style>
  /* Page-header title + (i) clustered on the left, timestamp pushed
     right by .page-header's existing space-between. Mirrors the
     pattern on /admin/options (Derivatives). */
  .pulse-title-group {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
</style>
