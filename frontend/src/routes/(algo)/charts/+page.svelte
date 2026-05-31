<script>
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { nowStamp } from '$lib/stores';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';

  // ── URL params ────────────────────────────────────────────────────
  let _symbol       = $state('');
  let _chartLoading = $state(false);
  let _error        = $state('');
  let _bump         = $state(0);

  // Default symbol when the page lands with no ?symbol= param. The
  // operator can swap to any pinned chip or pick a fresh symbol via
  // the type-filter + search box inside ChartWorkspace.
  const DEFAULT_SYMBOL = 'NIFTY 50';

  // ── Order modal ───────────────────────────────────────────────────
  let _orderModalOpen = $state(false);
  function _openOrderModal() {
    if (!_symbol) return;
    _orderModalOpen = true;
  }

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
      <InfoHint popup align="left"
        text="OHLCV historical chart (1D–1Y) + intraday tick overlay for any symbol. Switch between Line, Area, and Candle views. Toggle SMA20/SMA50/Vol overlays. Options show a Greeks strip below the chart. Wheel to zoom, drag to pan, Reset to restore the full range." />
    </span>
    <span class="algo-ts">{$nowStamp}</span>
    <span class="ml-auto"></span>
    <button class="page-order-btn" disabled={!_symbol} title="Place order — {_symbol || '—'}"
            onclick={_openOrderModal}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    </button>
    <RefreshButton onClick={_refresh} loading={_chartLoading} label="charts" />
    <PageHeaderActions hideOrder={true} hideChart={true} />
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

{#if _orderModalOpen}
  <SymbolPanel
    symbol={_symbol}
    exchange=""
    defaultTab="ticket"
    accounts={[]}
    account=""
    defaultMode="paper"
    availableModes={['draft', 'paper', 'live']}
    showChartButton={false}
    onClose={() => { _orderModalOpen = false; }}
    onSubmit={(_payload) => { _orderModalOpen = false; }}
  />
{/if}

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

  .page-order-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    border: 1px solid rgba(251, 191, 36, 0.55);
    background: rgba(251, 191, 36, 0.14);
    color: #fbbf24;
    border-radius: 3px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .page-order-btn:hover:not(:disabled) {
    background: rgba(251, 191, 36, 0.22);
    border-color: rgba(252, 211, 77, 0.65);
    color: #fcd34d;
  }
  .page-order-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  @media (max-width: 600px) {
    .charts-page-wrap {
      height: calc(100vh - 3rem - 1.5rem);
    }
  }
</style>
