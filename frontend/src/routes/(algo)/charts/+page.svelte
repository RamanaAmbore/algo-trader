<script>
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import { resolveSymbol, setRecentSymbol } from '$lib/data/accounts';
  import { chartStore } from '$lib/data/chartStore.svelte.js';

  // ── URL params ────────────────────────────────────────────────────
  let _symbol       = $state('');
  let _showLiveTs = $state(false);
  let _chartLoading = $state(false);
  let _error        = $state('');
  let _bump         = $state(0);

  // Default symbol when the page lands with no ?symbol= param.
  // Resolution chain: ?symbol= → recent → settings default →
  // NIFTY 50. Operator: "let orders page and chart page use
  // default symbol if there is no recent symbol is used in charts
  // or orders. if any symbol is used ... the symbol should be
  // defaulted to that."
  const _FALLBACK_SYMBOL = 'NIFTY 50';

  function _initFromUrl() {
    const params = page.url.searchParams;
    const sym = params.get('symbol');
    if (sym) _symbol = String(sym).toUpperCase();
  }

  function _refresh() {
    if (!_symbol) return;   // nothing to refresh until operator picks
    _error = '';
    _bump++;
    // _chartLoading reflects ChartWorkspace's _histLoading via $bindable — no
    // need to set it here; the bound prop updates as the chart loads.
  }

  function _onSymbolChange(/** @type {string} */ sym) {
    const s = String(sym || '').toUpperCase();
    _symbol = s;
    // Keep the store in sync so ChartModal (if open) and any other
    // surface reading chartStore immediately sees the new symbol.
    chartStore.setSymbol(s);
    if (s) setRecentSymbol(s);
    const url = new URL(page.url);
    url.searchParams.set('symbol', s);
    goto(url.toString(), { replaceState: true, noScroll: true, keepFocus: true });
  }

  onMount(() => {
    _initFromUrl();
    // If no URL param but the store already has a symbol (e.g. operator
    // just closed ChartModal), pre-fill from the store so the chart
    // shows the same symbol instantly — no refetch if data is fresh.
    if (!_symbol && chartStore.symbol) _symbol = chartStore.symbol;
    if (!_symbol) _symbol = resolveSymbol(_FALLBACK_SYMBOL);
    if (_symbol) {
      setRecentSymbol(_symbol);
      // Seed the store so ChartWorkspace.onMount isFresh() check has
      // the right key before the first _loadHistorical runs.
      chartStore.setSymbol(_symbol);
    }
  });
</script>

<svelte:head>
  <title>Charts | RamboQuant Analytics</title>
</svelte:head>

<div class="charts-page-wrap">
  <!-- Page header -->
  <div class="page-header">
    <span class="algo-title-group">
      <h1 class="page-title-chip">Charts</h1>
    </span>
    <span class="algo-ts-group">
      <span class="algo-ts" class:algo-ts-hidden={_showLiveTs}
            onclick={() => _showLiveTs = !_showLiveTs}
            title="Live clock — tap to switch" role="button" tabindex="0"
            onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
        {$nowStamp}
      </span>
      <span class="algo-ts-vsep" aria-hidden="true">|</span>
      <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}
            onclick={() => _showLiveTs = !_showLiveTs}
            title="Last refresh — tap to switch" role="button" tabindex="0"
            onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
        {formatDualTz($lastRefreshAt)}
      </span>
    </span>
    <span class="ml-auto"></span>
    <span class="page-header-actions">
      <RefreshButton onClick={_refresh} loading={_chartLoading} label="charts" />
      <!-- Order icon stays visible (operator: "charts should not have
           chart icon" — only Chart is suppressed since the page IS
           the chart). PageHeaderActions reuses the same SymbolPanel
           modal the retired custom .page-order-btn did, pre-filled
           with the page's active symbol. Log + Order icons read the
           same shape they read on every other algo page. -->
      <PageHeaderActions symbol={_symbol} hideChart={true} />
    </span>
  </div>

  {#if _error}
    <div class="page-error">{_error}</div>
  {/if}

  <div class="chart-body">
    <ChartWorkspace
      bind:symbol={_symbol}
      bind:loading={_chartLoading}
      compact={false}
      showHeader={false}
      bump={_bump}
      onSymbolChange={_onSymbolChange}
    />
  </div>
</div>


<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  /* Charts page fills the full viewport below the sticky navbar.
     Algo-content is itself a flex column with min-height: 0 + padding
     calc(3rem + 1.8rem) top + calc(1.6rem + 0.4rem) bottom (or +1.5rem
     when ps-strip is present via :has() override). We rely on that
     flex chain rather than a fixed calc — earlier `calc(100vh - 3rem
     - 2rem)` over-counted by ~3 rem and produced a vertical scrollbar
     on phone viewports (operator: "the entire chart grid should fit
     in mobile viewport with no scrolling"). `flex: 1 1 0; min-height:
     0; overflow: hidden;` chain lets the wrap absorb whatever
     algo-content offers and the SVG below claims every residual
     pixel — independent of whether ps-strip is mounted. */
  .charts-page-wrap {
    display: flex;
    flex-direction: column;
    flex: 1 1 0;
    min-height: 0;
    gap: 0.2rem;
    padding: 0;
    box-sizing: border-box;
    overflow: hidden;
  }

  .page-error {
    color: var(--c-short);
    font-size: var(--fs-md);
    font-family: monospace;
    padding: 0 0.25rem;
    flex-shrink: 0;
  }

  .chart-body {
    flex: 1 1 0;
    min-height: 0;          /* critical — lets flex child overflow instead of parent */
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  @media (max-width: 600px) {
    .charts-page-wrap {
      gap: 0.15rem;
    }
  }
</style>
