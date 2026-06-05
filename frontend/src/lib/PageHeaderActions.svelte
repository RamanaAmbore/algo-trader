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

  import { getContext, onMount } from 'svelte';
  import { executionMode } from '$lib/stores';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import ChartModal from '$lib/ChartModal.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import { resolveAnchorToTradeable } from '$lib/data/resolveUnderlying';
  import { findNearestFuture, loadInstruments } from '$lib/data/instruments';
  import { loadAccounts, getDefaultSymbol, defaultSymbolStore, accountsReadyStore } from '$lib/data/accounts';

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
     *  When empty the trio falls back to NIFTY 50 so the chart / order
     *  modal still opens with something pre-selected. Pages that have a
     *  contextual symbol (Options selectedUnderlying, Orders entry
     *  symbol, Research selected.symbol) override this via the prop. */
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

  // Universal default falls back to the operator-configured
  // orders.default_symbol setting (CRUDEOIL by default). The
  // defaultSymbolStore is set the moment loadAccounts() resolves
  // (accounts.js kicks off the fetch at module-load time so by the
  // time the operator clicks the Order button, the value is already
  // here). Falls through to NIFTY 50 ONLY after the store has emitted
  // and the operator's setting is blank — eliminates the prior race
  // where the hardcoded NIFTY 50 fallback briefly leaked into the
  // modal before the setting fetch landed.
  const _operatorDefault = $derived(String($defaultSymbolStore || '').toUpperCase());
  const _accountsReady   = $derived($accountsReadyStore);
  // Kick the fetch alongside (idempotent). loadAccounts memoises so the
  // module-level call + this one don't duplicate the network round-trip.
  onMount(() => { loadAccounts().catch(() => {}); });
  const _anchorSymbol      = $derived(
    // Caller-supplied symbol wins. Otherwise: operator default once
    // the store has populated; before that, leave the anchor empty
    // so the modal doesn't latch onto a stale fallback. The 'NIFTY 50'
    // fall-through ONLY kicks in after the store has resolved with a
    // blank operator default (anonymous demo / setting cleared).
    String(symbol || _operatorDefault || (_accountsReady ? 'NIFTY 50' : '')).toUpperCase()
  );
  const _effectiveExchange = $derived(String(exchange || (symbol ? '' : 'NSE')));

  // Resolve the anchor to a tradeable contract (NIFTY 50 → NIFTY26JUNFUT,
  // CRUDEOIL → CRUDEOILM26JUNFUT, RELIANCE → RELIANCE) so both modals
  // open with the actual future / option / equity tradingsymbol — not
  // the spot quote-key or commodity root, which are non-tradeable and
  // make the historical endpoint walk every exchange searching for a
  // contract that doesn't exist.
  let _resolvedSymbol = $state('');
  $effect(() => {
    const anchor = _anchorSymbol;
    if (!anchor) { _resolvedSymbol = ''; return; }
    // Sync first — instruments cache is usually warm.
    let tradeable = resolveAnchorToTradeable(anchor, findNearestFuture);
    if (tradeable) { _resolvedSymbol = tradeable; return; }
    // Cold cache — hydrate then retry, fall back to anchor on failure.
    _resolvedSymbol = anchor;
    (async () => {
      try {
        await loadInstruments();
        const t = resolveAnchorToTradeable(anchor, findNearestFuture);
        if (t && t !== anchor) _resolvedSymbol = t;
      } catch (_) { /* leave _resolvedSymbol at the anchor */ }
    })();
  });

  const _effectiveSymbol = $derived(_resolvedSymbol || _anchorSymbol);

  // ── Internal modal state ──────────────────────────────────────────────
  let _orderOpen = $state(false);
  let _chartOpen = $state(false);
  let _logOpen   = $state(false);

  async function _openOrder() {
    _chartOpen = false;
    _logOpen   = false;
    // If the operator clicks before the default-symbol store has
    // resolved, wait briefly so the modal opens with the right anchor
    // (e.g. CRUDEOIL) rather than the empty fallback that would later
    // race-flip. loadAccounts() is memoised + already in flight from
    // accounts.js's module-load kick-off.
    if (!_accountsReady) {
      try { await loadAccounts(); } catch (_) { /* open anyway */ }
    }
    _orderOpen = true;
  }

  async function _openChart() {
    // Always open the ChartModal inline — the modal carries its own
    // symbol search + pinned-dropdown so the operator can pick any
    // symbol from inside. Same default-symbol wait as _openOrder so
    // the chart opens against the right anchor on first invoke.
    _orderOpen = false;
    _logOpen   = false;
    if (!_accountsReady) {
      try { await loadAccounts(); } catch (_) { /* open anyway */ }
    }
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
  <!-- Tooltips + aria-labels mirror the modal titles ("Orders" /
       "Charts" / "Activity"), which in turn mirror the page-route
       names (/orders, /charts, /agents/activity). Earlier the order
       button said "Place order" and the chart button said "Chart —
       …", which read as a different surface from the modal that
       actually opens. -->
  {#if !hideOrder}
    <button type="button" class="pha-btn pha-order"
            onclick={_openOrder}
            title="Orders{symbol ? ` — ${symbol}` : ''}"
            aria-label="Open Orders">
      <!-- Order-slip / receipt glyph — a small rectangle with order
           lines inside. Reads as "open the order entry form" without
           the directional + the prior dual-arrow had (operator said
           that read as a refresh icon). Same family as the canonical
           "form / document" icons used by IBKR TWS and Sensibull. -->
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <rect x="3.2" y="2" width="9.6" height="12" rx="1.2"
              stroke="currentColor" stroke-width="1.5"/>
        <path d="M5.5 6h5M5.5 8.5h5M5.5 11h3"
              stroke="currentColor" stroke-width="1.4"
              stroke-linecap="round"/>
      </svg>
    </button>
  {/if}

  {#if !hideChart}
    <button type="button" class="pha-btn pha-chart"
            onclick={_openChart}
            title="Charts{symbol ? ` — ${symbol}` : ''}"
            aria-label="Open Charts">
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
            title="Activity"
            aria-label="Open Activity">
      <!-- 3D bell glyph — operator: "keep fast bell icon to activity
           header also". Same dimensional bell rendered on the
           ActivityLogModal title (radial gradient body, dark outline,
           specular highlight, drop shadow), mirrored on this page-
           header button so opening Activity from either entry point
           gets the same identity glyph. Orange palette per
           operator: "change cyan color to something else for bell
           icon notification" — bell becomes the classical warm
           notification color, distinct from the violet that read
           as too cool-tone. -->
      <!-- Operator: "make bell 3D icon less 3D decoration". Flattened
           the radial gradient to a near-solid orange (very subtle
           top-to-bottom shift only). Outline stroke kept but very
           thin so it just defines the silhouette without adding
           dimensional shading. Reads as a flat-but-friendly bell
           icon, not a rendered object. -->
      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
        <path d="M8 2c-2.4 0-4 1.9-4 4.2 0 2.1-.8 3.6-1.7 4.5-.3.3-.1.8.3.8h10.8c.4 0 .6-.5.3-.8-.9-.9-1.7-2.4-1.7-4.5C12 3.9 10.4 2 8 2z"
              fill="#fb923c"
              stroke="#9a3412" stroke-width="0.4"
              stroke-linejoin="round" />
        <path d="M6.6 13c.2.8.8 1.3 1.4 1.3.6 0 1.2-.5 1.4-1.3z"
              fill="#fb923c" stroke="#9a3412" stroke-width="0.4"
              stroke-linejoin="round" />
        <circle cx="8" cy="2.1" r="0.55" fill="#fb923c"
                stroke="#9a3412" stroke-width="0.4" />
      </svg>
    </button>
  {/if}
</span>

<!-- ── Modals ──────────────────────────────────────────────────────── -->
{#if _orderOpen}
  <!-- Order modal mirrors the /orders entry shell — same SymbolPanel
       tab content (Order ticket / Chain / Command line). The bottom
       Order Book + Agent Log panel is rendered so the operator can
       see live order/agent activity from inside the modal (operator
       feedback: "Orders modal should always show the logs tab at the
       bottom"). The × close stays at the top-right of SymbolPanel's
       own header; chart icon hidden since every page already has one
       in its own header. -->
  <SymbolPanel
    symbol={_effectiveSymbol}
    exchange={_effectiveExchange}
    defaultTab="chain"
    accounts={[]}
    account=""
    defaultMode={$executionMode === 'live' && _effectiveModes.includes('live') ? 'live' : 'paper'}
    availableModes={_effectiveModes}
    onClose={() => { _orderOpen = false; }}
    onSubmit={() => { _orderOpen = false; }}
  />
{/if}

{#if _chartOpen}
  <ChartModal
    symbol={_effectiveSymbol}
    exchange={_effectiveExchange}
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

  /* ── Log / Activity button — muted orange ────────────────────
     Operator: "change cyan color to something else for bell icon
     notification". Switched from violet to orange — classical
     notification warm-tone, distinct from the Order button's
     amber-yellow so the three buttons (amber-yellow Order, cyan
     Chart, orange Activity) stay visually separable. */
  .pha-log {
    border: 1px solid rgba(251, 146, 60, 0.45);
    color: #fb923c;
  }
  .pha-log:hover:not(:disabled) {
    background: rgba(251, 146, 60, 0.14);
    border-color: rgba(253, 186, 116, 0.70);
    color: #fdba74;
  }
</style>
