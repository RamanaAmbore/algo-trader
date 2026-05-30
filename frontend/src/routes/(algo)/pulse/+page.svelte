<script>
  // Thin page wrapper — the unified market-pulse component lives in
  // $lib/MarketPulse.svelte. Both /pulse and /dashboard compose it
  // with different presets so the merge engine + symbol-cell renderer
  // + format helpers stay in one place.
  import MarketPulse from '$lib/MarketPulse.svelte';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import AgentNotifications from '$lib/AgentNotifications.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import { nowStamp } from '$lib/stores';

  // bind:this handle so the header's RefreshButton can trigger the
  // same refreshAllNow() path the per-card buttons use. Page owns
  // its own `_refreshing` flag — MarketPulse's internal state isn't
  // reactively observable from outside, so we wrap the call.
  let pulseRef = $state(/** @type {any} */ (null));
  let _refreshing = $state(false);
  async function refreshPage() {
    if (_refreshing || !pulseRef) return;
    _refreshing = true;
    try { await pulseRef.refresh(); }
    finally { _refreshing = false; }
  }
</script>

<svelte:head><title>Pulse | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="pulse-title-group">
    <h1 class="page-title-chip">Pulse</h1>
    <InfoHint popup text={'Live broker book — positions, holdings, watchlist quotes, movers and pinned indices in one grid. Tap any row to open the order ticket. Use <b>Show…</b> to toggle sources. Account multiselect scopes positions + holdings (watchlists stay visible). The toolbar carries an immediate-refresh button; auto-refresh cadence is driven by <span class="font-mono">pulse.tick_interval_ms</span> in /admin/settings.'} />
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <RefreshButton onClick={refreshPage} loading={_refreshing} label="pulse" />
  <span class="ml-auto"></span>
  <OrderNotifications /><AgentNotifications />
</div>

<!-- accountPicker=true mounts a per-card Account MultiSelect inside
     each of the Positions and Holdings card headers. Empty selection
     on a card = "all accounts" for that card. Watchlist + option
     underlyings are not account-scoped so they remain visible. -->
<MarketPulse bind:this={pulseRef} title="Pulse" flat={true} accountPicker={true} />

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
