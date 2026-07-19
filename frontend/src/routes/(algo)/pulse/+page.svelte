<script>
  // Thin page wrapper — the unified market-pulse component lives in
  // $lib/MarketPulse.svelte. Both /pulse and /dashboard compose it
  // with different presets so the merge engine + symbol-cell renderer
  // + format helpers stay in one place.
  import MarketPulse from '$lib/MarketPulse.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';

  // bind:this handle so the header's RefreshButton can trigger the
  // same refreshAllNow() path the per-card buttons use. Page owns
  // its own `_refreshing` flag — MarketPulse's internal state isn't
  // reactively observable from outside, so we wrap the call.
  let pulseRef = $state(/** @type {any} */ (null));
  let _refreshing = $state(false);
  /**
   * @param {{ skipLtp?: boolean } | undefined} opts  passed by RefreshButton
   *   when both markets are closed: `{ skipLtp: true }` tells the fetchers
   *   to route with `?skip_ltp=1` so cash/margins/holdings refresh from
   *   the broker while LTPs stay frozen at the daily_book snapshot value.
   *   Undefined / no arg → legacy behaviour (skipLtp: false).
   */
  async function refreshPage(opts = undefined) {
    if (_refreshing || !pulseRef) return;
    _refreshing = true;
    try { await pulseRef.refresh(opts?.skipLtp === true); }
    finally { _refreshing = false; }
  }
</script>

<svelte:head><title>Pulse | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Pulse</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={refreshPage} loading={_refreshing} label="pulse" />
    <PageHeaderActions />
  </span>
</div>

<!-- accountPicker=true mounts a per-card Account MultiSelect inside
     each of the Positions and Holdings card headers. Empty selection
     on a card = "all accounts" for that card. Watchlist + option
     underlyings are not account-scoped so they remain visible.

     Per-account summary tables (Account | Day P&L | Day % | P&L | P&L %)
     live on /dashboard. /pulse stays focused on the per-symbol
     unified grid + per-card TOTAL pinned at the bottom. -->
<MarketPulse bind:this={pulseRef} title="Pulse" flat={true} accountPicker={true} />


