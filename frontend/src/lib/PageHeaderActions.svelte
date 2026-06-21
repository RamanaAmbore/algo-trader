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
  import { prefetchChartBars } from '$lib/ChartWorkspace.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  // Symbol-resolver imports retired — operators pick tradeable
  // symbols directly (no NIFTY 50 → NIFTY26JUNFUT mapping). Operator:
  // "we don't have symbol and resolver concept now."
  import {
    loadAccounts,
    accountsReadyStore,
    recentSymbolStore,
    resolveAccount,
    getAccountsSync,
  } from '$lib/data/accounts';
  import BellIcon from '$lib/icons/BellIcon.svelte';

  // Phase B: availableModes / defaultMode no longer passed to SymbolPanel.
  // Mode is read from the global executionMode store (set via navbar dropdown).
  const _algoStatus = getContext('algoStatus');

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
  const _recentSymbol  = $derived(String($recentSymbolStore || '').toUpperCase());
  const _accountsReady = $derived($accountsReadyStore);
  // Kick the fetch alongside (idempotent). loadAccounts memoises so the
  // module-level call + this one don't duplicate the network round-trip.
  onMount(() => { loadAccounts().catch(() => {}); });
  // Symbol resolution chain (operator: "Remove crudeoil symbol as
  // default symbol ... The symbol should be updated from the latest
  // symbol used or clear from the context for modals"):
  //   1. caller-supplied symbol (page contextual)
  //   2. recently-used symbol (operator's last pick on /orders,
  //      /charts, or any modal)
  //   3. empty — the modal opens with no symbol latched.
  const _anchorSymbol = $derived(
    String(symbol || _recentSymbol || '').toUpperCase()
  );
  const _effectiveExchange = $derived(String(exchange || (symbol ? '' : 'NSE')));

  // Effective symbol passes through verbatim — no underlying-to-future
  // resolver layer. Operator picks what they want to trade; that's
  // what the modal opens against.
  const _effectiveSymbol = $derived(_anchorSymbol);
  // Account resolution chain mirrors the symbol chain:
  //   1. recent-account store (operator's last pick)
  //   2. orders.default_account setting
  //   3. first loaded broker account
  // Operator: "order entry in modal and page don't have account drop
  // to default from context" — the modal was passing `account=""`
  // forcing SymbolPanel to fall through to settings/sole-account only.
  // Now the modal carries the recent context just like /orders does.
  const _accountsList = $derived(
    ($accountsReadyStore
      ? getAccountsSync().map(a => String(a?.account_id || a?.account || a || '')).filter(Boolean)
      : []),
  );
  const _effectiveAccount = $derived(
    resolveAccount(_accountsList[0] || ''),
  );

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
       names (/orders, /charts, /automation/activity). Earlier the order
       button said "Place order" and the chart button said "Chart —
       …", which read as a different surface from the modal that
       actually opens. -->
  {#if !hideOrder}
    <button type="button" class="pha-btn pha-order"
            onclick={_openOrder}
            title="Orders{symbol ? ` — ${formatSymbol(symbol)}` : ''}"
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
    <!-- onmouseenter / onfocus prefetch warms ChartWorkspace's module
         cache so the click-to-open hits memory. Operator: "I see
         delay chart plotting first time when I open the modal." -->
    <button type="button" class="pha-btn pha-chart"
            onclick={_openChart}
            onmouseenter={() => { if (_effectiveSymbol) prefetchChartBars(_effectiveSymbol, _effectiveExchange); }}
            onfocus={() => { if (_effectiveSymbol) prefetchChartBars(_effectiveSymbol, _effectiveExchange); }}
            title="Charts{symbol ? ` — ${formatSymbol(symbol)}` : ''}"
            aria-label="Open Charts">
      <!-- Polyline chart glyph -->
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <path d="M2 13h12M3 11l3-4 3 2 4-6" stroke="currentColor" stroke-width="1.9"
              stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </button>
  {/if}

  <button type="button" class="pha-btn pha-log"
            onclick={_openLog}
            title="Activity"
            aria-label="Open Activity">
      <!-- Shared BellIcon component — flat orange (#fb923c) with amber-700
           (#b45309) outline. Replaces the inlined SVG with hard-coded #9a3412
           that was duplicated across PageHeaderActions + ActivityLogModal. -->
      <BellIcon width="14" height="14" />
    </button>
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
  <!-- Operator: "on cold start, buy is still active." The navbar Order
       button is always a "compose new order" affordance — operator
       picks the side. Even when `_effectiveSymbol` is non-empty (from
       the recent-symbol store), the SIDE is the operator's choice, not
       inherited from the recent pick. The symbol-row signal flows
       through `currentQty != 0` (LogPanel modify/repeat path on
       /orders), NOT through PageHeaderActions. So always pass null
       here; SymbolPanel handles the inline-side-toggle from there. -->
  <SymbolPanel
    symbol={_effectiveSymbol}
    exchange={_effectiveExchange}
    side={null}
    defaultTab="ticket"
    accounts={_accountsList}
    account={_effectiveAccount}
    onClose={() => { _orderOpen = false; }}
    onSubmit={() => { setTimeout(() => { _orderOpen = false; }, 1500); }}
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
    /* 1.4rem matches RefreshButton so every chip in the page-header
       strip has the same height — strip stays predictable at
       ~1.8rem total (1.4rem buttons + 0.2rem pad both sides + 1px
       border) and padding-top can match exactly. */
    width: 1.4rem;
    height: 1.4rem;
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
