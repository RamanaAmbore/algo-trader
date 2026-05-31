<script>
  import { onMount, getContext } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { nowStamp } from '$lib/stores';
  import { fetchChartSymbols } from '$lib/api';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import AgentNotifications from '$lib/AgentNotifications.svelte';
  import InfoHint from '$lib/InfoHint.svelte';

  // ── Context ───────────────────────────────────────────────────────
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);

  // ── Card state ────────────────────────────────────────────────────
  let _colChart  = $state(false);
  let _fsChart   = $state(false);

  // ── URL params ────────────────────────────────────────────────────
  // Read symbol + mode from ?symbol=…&mode=… on first load.
  // Updates are pushed back to the URL via goto() when the operator
  // picks a new symbol from the ChartWorkspace picker.
  let _symbol = $state('');
  let _mode   = $state(/** @type {'live'|'sim'|'paper'} */('live'));
  let _loading = $state(false);
  let _error   = $state('');
  let _bump    = $state(0);

  function _initFromUrl() {
    const params = page.url.searchParams;
    const sym  = params.get('symbol');
    const mode = params.get('mode');
    if (sym) _symbol = sym.toUpperCase();
    if (mode === 'sim' || mode === 'paper' || mode === 'live') _mode = mode;
  }

  async function _loadDefaultSymbol() {
    if (_symbol) return;
    // Try to pick the first symbol with captured ticks from the live mode.
    try {
      const r = await fetchChartSymbols('live');
      const syms = Array.isArray(r) ? r : (r?.symbols || []);
      if (syms.length) {
        _symbol = String(syms[0]?.symbol || syms[0] || '').toUpperCase();
      }
    } catch (_) { /* silent */ }
    // Fallback to NIFTY 50 (space intentional — matches Kite instrument name).
    if (!_symbol) _symbol = 'NIFTY 50';
  }

  async function _refresh() {
    _loading = true;
    _error = '';
    try {
      // Re-load default if still empty (e.g. after a broker reconnect)
      if (!_symbol) await _loadDefaultSymbol();
      _bump++;  // force ChartWorkspace to reload its own data
    } catch (e) {
      _error = /** @type {any} */ (e)?.message || 'Refresh failed';
    } finally {
      _loading = false;
    }
  }

  function _onSymbolChange(/** @type {string} */ sym) {
    _symbol = sym;
    // Push to URL without triggering a full navigation / data reload.
    const url = new URL(page.url);
    url.searchParams.set('symbol', sym);
    goto(url.toString(), { replaceState: true, noScroll: true, keepFocus: true });
  }

  onMount(async () => {
    _initFromUrl();
    if (!_symbol) await _loadDefaultSymbol();
  });
</script>

<svelte:head>
  <title>Charts | RamboQuant Analytics</title>
</svelte:head>

<div class="algo-page">
  <!-- Page header -->
  <div class="page-header">
    <span class="algo-title-group">
      <h1 class="page-title-chip">Charts</h1>
      <InfoHint popup align="left"
        text="OHLCV historical chart (1D–1Y) + intraday tick overlay for any symbol. Switch between Line, Area, and Candle views. Toggle SMA20/SMA50/Vol overlays. Options show a Greeks strip below the chart. Wheel to zoom, drag to pan, Reset to restore the full range." />
    </span>
    <span class="algo-ts">{$nowStamp}</span>
    <span class="ml-auto"></span>
    <RefreshButton onClick={_refresh} loading={_loading} label="charts" />
    <OrderNotifications />
    <AgentNotifications />
  </div>

  {#if _error}
    <div class="page-error">{_error}</div>
  {/if}

  <!-- Charts card -->
  <div class="bucket-card" class:is-fullscreen={_fsChart} class:fs-card-on={_fsChart}>
    <div class="bucket-header">
      <span class="mp-section-label">
        {_symbol || 'Chart'}
        {#if isDemo}<span class="demo-badge">DEMO</span>{/if}
      </span>
      <!-- Card-control trio (right-aligned via ml-auto on the first button) -->
      <CollapseButton bind:isCollapsed={_colChart} cardId="charts-main" label="chart" />
      <DefaultSizeButton bind:isFullscreen={_fsChart} bind:isCollapsed={_colChart} label="chart" />
      <FullscreenButton bind:isFullscreen={_fsChart} label="chart" />
    </div>

    {#if !_colChart}
      <div class="bucket-body">
        <ChartWorkspace
          bind:symbol={_symbol}
          bind:mode={_mode}
          compact={false}
          showHeader={false}
          bump={_bump}
          onSymbolChange={_onSymbolChange}
        />
      </div>
    {/if}
  </div>
</div>

<style>
  .algo-page {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    padding: 0 0 2rem;
  }

  .page-error {
    color: #f87171;
    font-size: 0.65rem;
    font-family: monospace;
    padding: 0 0.25rem;
  }

  .bucket-card {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251, 191, 36, 0.18);
    border-radius: 6px;
    width: 100%;
    box-sizing: border-box;
  }

  .bucket-card.is-fullscreen {
    position: fixed;
    inset: 0;
    z-index: 100;
    border-radius: 0;
    overflow-y: auto;
  }

  .bucket-header {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.45rem 0.75rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    min-height: 2.2rem;
  }

  .mp-section-label {
    font-family: monospace;
    font-size: 0.65rem;
    font-weight: 700;
    color: #7dd3fc;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .demo-badge {
    font-size: 0.5rem;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid rgba(167, 139, 250, 0.45);
    background: rgba(167, 139, 250, 0.12);
    color: #a78bfa;
    font-family: monospace;
    font-weight: 700;
    letter-spacing: 0.05em;
  }

  /* .bucket-body: ChartWorkspace manages its own internal spacing */

  @media (max-width: 600px) {
    .bucket-header { padding: 0.35rem 0.5rem; }
  }
</style>
