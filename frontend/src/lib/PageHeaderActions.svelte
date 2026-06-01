<script>
  /**
   * PageHeaderActions — three vibrant action icons for every algo page header.
   *
   * Order  (amber)  → opens SymbolPanel (order ticket)
   * Chart  (cyan)   → opens ChartModal  (price chart)
   * Log    (violet) → opens ActivityLogModal (order book + agent log)
   *
   * Replaces the <OrderNotifications /> + <AgentNotifications /> bell pair.
   * AgentToast (auto-fire toast) is a separate concern and is NOT replaced here.
   */

  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import ChartModal from '$lib/ChartModal.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';

  let {
    /** Default symbol to pre-fill the Order + Chart modals.
     *  When empty the order modal opens with no symbol; chart button is disabled. */
    symbol    = /** @type {string} */ (''),
    /** Default exchange hint for the modals. */
    exchange  = /** @type {string} */ (''),
    /** When true, the Order icon is hidden (caller is on the orders page). */
    hideOrder = /** @type {boolean} */ (false),
    /** When true, the Chart icon is hidden (caller is on the charts page). */
    hideChart = /** @type {boolean} */ (false),
    /** When false, the Log icon is hidden. Defaults to shown. */
    showLog   = /** @type {boolean} */ (true),
  } = $props();

  // ── Internal modal state ──────────────────────────────────────────────
  let _orderOpen = $state(false);
  let _chartSym  = $state('');
  let _chartExch = $state('');
  let _logOpen   = $state(false);

  function _openOrder() {
    _orderOpen = true;
  }

  function _openChart() {
    if (!symbol) return;
    _chartSym  = String(symbol  || '').toUpperCase();
    _chartExch = String(exchange || '');
  }

  function _openLog() {
    _logOpen = true;
  }
</script>

<!-- The three action icons — inline-flex so they sit flush in the
     page-header's row layout without extra wrapper margin. -->
<span class="pha-wrap">
  {#if !hideOrder}
    <button type="button" class="pha-btn pha-order"
            onclick={_openOrder}
            title="Place order{symbol ? ` — ${symbol}` : ''}"
            aria-label="Place order">
      <!-- Plus / Add glyph -->
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="2.2"
              stroke-linecap="round"/>
      </svg>
    </button>
  {/if}

  {#if !hideChart}
    <button type="button" class="pha-btn pha-chart"
            onclick={_openChart}
            disabled={!symbol}
            title={symbol ? `Chart — ${symbol}` : 'Pick a symbol first'}
            aria-label={symbol ? `Open chart for ${symbol}` : 'Open chart (no symbol)'}>
      <!-- Polyline chart glyph -->
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M2 13h12M3 11l3-4 3 2 4-6" stroke="currentColor" stroke-width="1.9"
              stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
  {/if}

  {#if showLog !== false}
    <button type="button" class="pha-btn pha-log"
            onclick={_openLog}
            title="Activity log — orders &amp; agent events"
            aria-label="Open activity log">
      <!-- Lines / list glyph -->
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M3 4h10M3 8h10M3 12h6" stroke="currentColor" stroke-width="1.9"
              stroke-linecap="round"/>
      </svg>
    </button>
  {/if}
</span>

<!-- ── Modals ──────────────────────────────────────────────────────── -->
{#if _orderOpen}
  <!-- Order modal mirrors the /orders entry shell — same SymbolPanel
       tab content (Order ticket / Chain / Command line), but stripped
       of header chrome that doesn't make sense in a quick-entry modal:
       chart icon (operator already has one click away on the page),
       watchlist add (no onAddToWatchlist wired), bottom Order Book +
       Order Log panel (noise inside a transient modal). The × close
       stays at the top-right of SymbolPanel's own header. -->
  <SymbolPanel
    symbol={String(symbol || '').toUpperCase()}
    exchange={String(exchange || '')}
    defaultTab="chain"
    accounts={[]}
    account=""
    defaultMode="paper"
    availableModes={['draft', 'paper', 'live']}
    showChartButton={false}
    hideBottomPanel={true}
    onClose={() => { _orderOpen = false; }}
    onSubmit={() => { _orderOpen = false; }}
  />
{/if}

{#if _chartSym}
  <ChartModal
    symbol={_chartSym}
    exchange={_chartExch}
    onClose={() => { _chartSym = ''; _chartExch = ''; }}
  />
{/if}

{#if _logOpen}
  <ActivityLogModal onClose={() => { _logOpen = false; }} />
{/if}

<style>
  /* Wrapper: keeps the three buttons as a tight cluster, aligned like
     the bell pair they replace (align-self: center so they sit at
     the mid-line, not the baseline, inside the page-header flex row). */
  .pha-wrap {
    display: inline-flex;
    align-items: center;
    align-self: center;
    gap: 0.3rem;
    flex-shrink: 0;
  }

  /* ── Base button ─────────────────────────────────────────────── */
  .pha-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.6rem;
    height: 1.6rem;
    padding: 0;
    border-radius: 4px;
    cursor: pointer;
    flex-shrink: 0;
    transition: transform 0.12s, filter 0.12s, box-shadow 0.12s;
  }
  .pha-btn:hover:not(:disabled) {
    transform: translateY(-1px);
    filter: brightness(1.18);
  }
  .pha-btn:active:not(:disabled) {
    transform: translateY(0);
    filter: brightness(0.95);
  }
  .pha-btn:disabled {
    opacity: 0.38;
    cursor: not-allowed;
  }
  /* Remove default focus ring; provide a tasteful custom one. */
  .pha-btn:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.45);
    outline-offset: 2px;
  }

  /* ── Order button — vivid amber ──────────────────────────────── */
  .pha-order {
    background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
    border: 1px solid rgba(252, 211, 77, 0.85);
    color: #1a1410;
    box-shadow: 0 2px 6px rgba(245, 158, 11, 0.38);
  }
  .pha-order:hover:not(:disabled) {
    box-shadow: 0 4px 10px rgba(245, 158, 11, 0.52);
  }

  /* ── Chart button — vivid cyan ───────────────────────────────── */
  .pha-chart {
    background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%);
    border: 1px solid rgba(103, 232, 249, 0.85);
    color: #0a1a1f;
    box-shadow: 0 2px 6px rgba(6, 182, 212, 0.38);
  }
  .pha-chart:hover:not(:disabled) {
    box-shadow: 0 4px 10px rgba(6, 182, 212, 0.52);
  }

  /* ── Log button — vivid violet ───────────────────────────────── */
  .pha-log {
    background: linear-gradient(135deg, #a855f7 0%, #7e22ce 100%);
    border: 1px solid rgba(216, 180, 254, 0.85);
    color: #1a0f24;
    box-shadow: 0 2px 6px rgba(168, 85, 247, 0.38);
  }
  .pha-log:hover:not(:disabled) {
    box-shadow: 0 4px 10px rgba(168, 85, 247, 0.52);
  }
</style>
