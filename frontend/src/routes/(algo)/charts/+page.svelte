<script>
  import { onMount, getContext } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { nowStamp } from '$lib/stores';
  import { fetchChartSymbols } from '$lib/api';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import AgentNotifications from '$lib/AgentNotifications.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';

  // ── Context ───────────────────────────────────────────────────────
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);

  // ── URL params ────────────────────────────────────────────────────
  let _symbol  = $state('');
  let _loading = $state(false);
  let _error   = $state('');
  let _bump    = $state(0);

  // Kite returns underlying index names ("NIFTY 50", "NIFTY BANK") via
  // the quote-key path; those are NOT tradingsymbols and won't fetch.
  // Map them back to their tradeable underlying tickers before use.
  const _KITE_INDEX_MAP = /** @type {Record<string, string>} */ ({
    'NIFTY 50':           'NIFTY',
    'NIFTY BANK':         'BANKNIFTY',
    'NIFTY FIN SERVICE':  'FINNIFTY',
    'NIFTY MID SELECT':   'MIDCPNIFTY',
    'NIFTY NEXT 50':      'NIFTYNXT50',
  });
  function _normalizeSymbol(/** @type {string} */ raw) {
    const s = String(raw || '').trim().toUpperCase();
    if (!s) return '';
    return _KITE_INDEX_MAP[s] ?? s;
  }

  // ── Order modal ───────────────────────────────────────────────────
  let _orderModalOpen = $state(false);
  function _openOrderModal() {
    if (!_symbol) return;
    _orderModalOpen = true;
  }

  function _initFromUrl() {
    const params = page.url.searchParams;
    const sym = params.get('symbol');
    if (sym) _symbol = _normalizeSymbol(sym);
  }

  async function _loadDefaultSymbol() {
    if (_symbol) return;
    try {
      const r = await fetchChartSymbols('live');
      const syms = Array.isArray(r) ? r : (r?.symbols || []);
      // Prefer captured symbols that look like real tradingsymbols
      // (no embedded space). Skip Kite-style index names so we don't
      // land on "NIFTY 50" as the chart's first impression.
      const picked = syms
        .map(/** @param {any} s */ (s) => String(s?.symbol || s || '').toUpperCase())
        .find(/** @param {string} s */ (s) => s && !s.includes(' '));
      if (picked) _symbol = picked;
    } catch (_) { /* silent */ }
    if (!_symbol) _symbol = 'NIFTY';
  }

  async function _refresh() {
    _loading = true;
    _error = '';
    try {
      if (!_symbol) await _loadDefaultSymbol();
      _bump++;
    } catch (e) {
      _error = /** @type {any} */ (e)?.message || 'Refresh failed';
    } finally {
      _loading = false;
    }
  }

  function _onSymbolChange(/** @type {string} */ sym) {
    const norm = _normalizeSymbol(sym);
    _symbol = norm;
    const url = new URL(page.url);
    url.searchParams.set('symbol', norm);
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

<div class="charts-page-wrap">
  <!-- Page header -->
  <div class="page-header">
    <span class="algo-title-group">
      <h1 class="page-title-chip">Charts</h1>
      {#if _symbol}
        <span class="chart-page-sym">{_symbol}</span>
      {/if}
      {#if isDemo}<span class="demo-badge">DEMO</span>{/if}
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
    <RefreshButton onClick={_refresh} loading={_loading} label="charts" />
    <OrderNotifications />
    <AgentNotifications />
  </div>

  {#if _error}
    <div class="page-error">{_error}</div>
  {/if}

  <div class="chart-body">
    <ChartWorkspace
      bind:symbol={_symbol}
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

  .chart-page-sym {
    font-family: monospace;
    font-size: 0.7rem;
    font-weight: 800;
    color: #fbbf24;
    letter-spacing: 0.05em;
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
