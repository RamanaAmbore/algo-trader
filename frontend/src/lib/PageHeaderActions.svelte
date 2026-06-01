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

  import { getContext } from 'svelte';
  import { executionMode } from '$lib/stores';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import ChartModal from '$lib/ChartModal.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';

  // HIGH 2: derive the set of mode pills the order ticket should show.
  // Restricts LIVE to authenticated prod sessions where the master toggle
  // is already set to LIVE — everywhere else the ticket shows draft+paper only.
  const _algoStatus = getContext('algoStatus');
  const _effectiveModes = $derived.by(() => {
    const ctx = _algoStatus;
    if (!ctx) return /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'paper']);
    if (ctx.isDemo) return /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'paper']);
    if (ctx.branch !== 'main') return /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'paper']);
    // On prod: surface LIVE only when the master execution mode is already live.
    if ($executionMode === 'live') return /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'paper', 'live']);
    return /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'paper']);
  });

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
  let _chartOpen = $state(false);
  let _logOpen   = $state(false);

  function _openOrder() {
    _chartOpen = false;
    _logOpen   = false;
    _orderOpen = true;
  }

  function _openChart() {
    if (!symbol) return;
    _orderOpen = false;
    _logOpen   = false;
    _chartOpen = true;
  }

  function _openLog() {
    _orderOpen = false;
    _chartOpen = false;
    _logOpen   = true;
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
            title={symbol ? `Chart — ${symbol}` : 'Chart'}
            aria-label={symbol ? `Open chart for ${symbol}` : 'Open chart'}>
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
    availableModes={_effectiveModes}
    showChartButton={false}
    hideBottomPanel={true}
    onClose={() => { _orderOpen = false; }}
    onSubmit={() => { _orderOpen = false; }}
  />
{/if}

{#if _chartOpen}
  <ChartModal
    symbol={String(symbol || '').toUpperCase()}
    exchange={String(exchange || '')}
    onClose={() => { _chartOpen = false; }}
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
    /* Mellow base — soft tinted background, no gradient, no shadow.
       The per-button accent colour lives on the border + icon stroke
       so the buttons read as a quiet trio rather than three saturated
       pills competing with the page-title chip. */
    background: rgba(255, 255, 255, 0.03);
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .pha-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .pha-btn:focus-visible {
    outline: 1px solid rgba(255, 255, 255, 0.40);
    outline-offset: 2px;
  }

  /* ── Order button — muted amber ──────────────────────────────── */
  .pha-order {
    border: 1px solid rgba(251, 191, 36, 0.40);
    color: #fbbf24;
  }
  .pha-order:hover:not(:disabled) {
    background: rgba(251, 191, 36, 0.12);
    border-color: rgba(252, 211, 77, 0.65);
    color: #fcd34d;
  }

  /* ── Chart button — muted cyan ───────────────────────────────── */
  .pha-chart {
    border: 1px solid rgba(34, 211, 238, 0.40);
    color: #22d3ee;
  }
  .pha-chart:hover:not(:disabled) {
    background: rgba(34, 211, 238, 0.12);
    border-color: rgba(103, 232, 249, 0.65);
    color: #67e8f9;
  }

  /* ── Log button — muted violet ───────────────────────────────── */
  .pha-log {
    border: 1px solid rgba(168, 85, 247, 0.40);
    color: #a855f7;
  }
  .pha-log:hover:not(:disabled) {
    background: rgba(168, 85, 247, 0.12);
    border-color: rgba(216, 180, 254, 0.65);
    color: #c084fc;
  }
</style>
