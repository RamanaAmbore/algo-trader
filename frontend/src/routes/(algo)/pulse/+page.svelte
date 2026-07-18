<script>
  // Thin page wrapper — the unified market-pulse component lives in
  // $lib/MarketPulse.svelte. Both /pulse and /dashboard compose it
  // with different presets so the merge engine + symbol-cell renderer
  // + format helpers stay in one place.
  import MarketPulse from '$lib/MarketPulse.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import { nowStamp } from '$lib/stores';

  // bind:this handle so the header's RefreshButton can trigger the
  // same refreshAllNow() path the per-card buttons use. Page owns
  // its own `_refreshing` flag — MarketPulse's internal state isn't
  // reactively observable from outside, so we wrap the call.
  let pulseRef = $state(/** @type {any} */ (null));
  let _refreshing = $state(false);
  let _moversAsOf = $state(/** @type {string|null} */ (null));
  let _showLiveTs = $state(false);
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
  <span class="algo-ts-group">
    {#if _moversAsOf}
      <span class="algo-ts algo-ts-data"
            class:algo-ts-hidden={_showLiveTs}
            onclick={() => { _showLiveTs = !_showLiveTs; }}
            title="Data as-of — tap to switch"
            role="button" tabindex="0"
            onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
        {_moversAsOf}
      </span>
      <span class="algo-ts-vsep" aria-hidden="true">|</span>
    {/if}
    <span class="algo-ts"
          class:algo-ts-hidden={!!_moversAsOf && !_showLiveTs}
          onclick={() => { if (_moversAsOf) _showLiveTs = !_showLiveTs; }}
          onkeydown={(e) => { if (_moversAsOf && e.key === 'Enter') _showLiveTs = !_showLiveTs; }}
          role="button"
          tabindex="0"
          title={_moversAsOf ? 'Live clock — tap to switch' : undefined}>
      {$nowStamp}
    </span>
  </span>
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
<MarketPulse bind:this={pulseRef} title="Pulse" flat={true} accountPicker={true} bind:moversAsOf={_moversAsOf} />

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) {
    .algo-ts-hidden { display: none !important; }
  }
</style>

