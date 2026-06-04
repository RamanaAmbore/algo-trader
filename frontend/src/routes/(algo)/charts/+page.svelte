<script>
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { nowStamp } from '$lib/stores';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';

  // ── URL params ────────────────────────────────────────────────────
  let _symbol       = $state('');
  let _chartLoading = $state(false);
  let _error        = $state('');
  let _bump         = $state(0);

  // Default symbol when the page lands with no ?symbol= param. The
  // operator can swap to any pinned chip or pick a fresh symbol via
  // the type-filter + search box inside ChartWorkspace.
  const DEFAULT_SYMBOL = 'NIFTY 50';

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
    const url = new URL(page.url);
    url.searchParams.set('symbol', s);
    goto(url.toString(), { replaceState: true, noScroll: true, keepFocus: true });
  }

  onMount(() => {
    _initFromUrl();
    if (!_symbol) _symbol = DEFAULT_SYMBOL;
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
    <span class="algo-ts">{$nowStamp}</span>
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
  /* Charts page fills the full viewport below the sticky navbar.
     The navbar is h-12 = 3rem; algo-content adds 0.5rem top padding.
     The page-header itself is roughly 2.2rem. We use flexbox so the
     chart-body absorbs whatever remains rather than a fixed calc. */
  .charts-page-wrap {
    display: flex;
    flex-direction: column;
    /* Full viewport height minus navbar (3rem) minus content top-pad (0.5rem)
       minus bottom-pad (1.5rem from algo-content). The flex column + flex:1
       on chart-body does the real work; this just caps the outer shell. */
    height: calc(100vh - 3rem - 2rem);
    min-height: 24rem;
    gap: 0.4rem;
    padding: 0 0 0.25rem;
    box-sizing: border-box;
  }

  .page-error {
    color: #f87171;
    font-size: 0.65rem;
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
      height: calc(100vh - 3rem - 1.5rem);
    }
  }
</style>
