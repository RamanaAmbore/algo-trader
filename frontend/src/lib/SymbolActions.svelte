<script>
  // SymbolActions — reusable popover with three affordances:
  //   📈  open the symbol's price chart in a modal
  //   ★   add to the operator's default watchlist
  //   📝  open the OrderTicket pre-filled to trade
  //
  // Used wherever the platform lists a symbol the operator might want
  // to act on — MarketPulse positions/holdings rows, the Options
  // candidates list, the Lab activity feed. Renders as a compact
  // three-dot trigger that pops a menu on click; the parent supplies
  // the action callbacks (so this component is purely presentational
  // — no API logic, no global state).
  //
  // Why a single shared component instead of one-off buttons per
  // page: every list of symbols had to re-implement the same three
  // affordances, and they drifted (some pages had +W, some didn't;
  // some had Chart, some opened a different surface). One component
  // = one consistent operator vocabulary across every list.

  import { onMount, onDestroy } from 'svelte';

  /** @type {{
   *   symbol:           string,
   *   exchange?:        string,
   *   onOpenChart?:     ((sym: string, exch?: string) => void) | null,
   *   onAddToWatchlist?:((sym: string, exch?: string) => void) | null,
   *   onOpenTicket?:    ((props: any) => void) | null,
   *   defaultTicketSide?: 'BUY' | 'SELL',
   *   defaultTicketAccount?: string,
   * }} */
  const { symbol, exchange = 'NFO',
          onOpenChart = null,
          onAddToWatchlist = null,
          onOpenTicket = null,
          defaultTicketSide = /** @type {'BUY'|'SELL'} */ ('BUY'),
          defaultTicketAccount = '' } = $props();

  let _open = $state(false);
  /** @type {{ msg: string, ok: boolean } | null} */
  let _toast = $state(null);
  let _toastTimer = /** @type {ReturnType<typeof setTimeout> | null} */ (null);
  let _trigger = /** @type {HTMLElement | undefined} */ (undefined);

  function _flash(/** @type {string} */ msg, /** @type {boolean} */ ok = true) {
    _toast = { msg, ok };
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { _toast = null; }, 1400);
  }

  // Close the menu when clicking outside.
  function _onDocClick(/** @type {MouseEvent} */ ev) {
    if (!_open) return;
    if (_trigger && _trigger.contains(/** @type {Node} */ (ev.target))) return;
    _open = false;
  }
  onMount(() => { document.addEventListener('mousedown', _onDocClick); });
  onDestroy(() => {
    document.removeEventListener('mousedown', _onDocClick);
    if (_toastTimer) clearTimeout(_toastTimer);
  });

  async function _doChart() {
    _open = false;
    if (!onOpenChart) return;
    try { await onOpenChart(symbol, exchange); }
    catch (e) { _flash(`Chart: ${/** @type {any} */ (e)?.message || 'failed'}`, false); }
  }
  async function _doWatch() {
    _open = false;
    if (!onAddToWatchlist) return;
    try {
      await onAddToWatchlist(symbol, exchange);
      _flash('✓ added to watchlist', true);
    } catch (e) {
      _flash(`Watchlist: ${/** @type {any} */ (e)?.message || 'failed'}`, false);
    }
  }
  function _doTrade() {
    _open = false;
    if (!onOpenTicket) return;
    onOpenTicket({
      symbol, exchange,
      side:    defaultTicketSide,
      action:  'open',
      account: defaultTicketAccount,
    });
  }

  // Don't render anything when zero actions are wired (parent
  // forgot to pass callbacks → no useful affordance anyway).
  const _hasAnyAction = $derived(
    !!onOpenChart || !!onAddToWatchlist || !!onOpenTicket,
  );
</script>

{#if _hasAnyAction}
  <span class="sa-wrap" bind:this={_trigger}>
    <button type="button" class="sa-trigger"
            class:on={_open}
            aria-haspopup="menu"
            aria-expanded={_open}
            aria-label={`Actions for ${symbol}`}
            title={`Actions for ${symbol}`}
            onclick={(e) => { e.stopPropagation(); _open = !_open; }}>⋯</button>

    {#if _open}
      <div class="sa-menu" role="menu">
        {#if onOpenChart}
          <button type="button" class="sa-item" role="menuitem"
                  onclick={_doChart}>
            <span class="sa-icon">📈</span> Chart
          </button>
        {/if}
        {#if onAddToWatchlist}
          <button type="button" class="sa-item" role="menuitem"
                  onclick={_doWatch}>
            <span class="sa-icon">★</span> Watchlist
          </button>
        {/if}
        {#if onOpenTicket}
          <button type="button" class="sa-item" role="menuitem"
                  onclick={_doTrade}>
            <span class="sa-icon">📝</span> Trade
          </button>
        {/if}
      </div>
    {/if}

    {#if _toast}
      <span class="sa-toast" class:ok={_toast.ok} class:err={!_toast.ok}>
        {_toast.msg}
      </span>
    {/if}
  </span>
{/if}

<style>
  .sa-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
  }
  .sa-trigger {
    border: 1px solid transparent;
    background: transparent;
    color: #7e97b8;
    cursor: pointer;
    padding: 0 0.25rem;
    font-size: 0.85rem;
    line-height: 1;
    border-radius: 3px;
  }
  .sa-trigger:hover, .sa-trigger.on {
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.32);
  }
  .sa-menu {
    position: absolute;
    top: calc(100% + 0.2rem);
    right: 0;
    z-index: 100;
    min-width: 9rem;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.32);
    border-radius: 4px;
    box-shadow: 0 4px 14px rgba(0,0,0,0.45);
    padding: 0.2rem;
    display: flex;
    flex-direction: column;
    gap: 0.05rem;
  }
  .sa-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.32rem 0.5rem;
    border: none;
    background: transparent;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    letter-spacing: 0.02em;
    cursor: pointer;
    border-radius: 2px;
    text-align: left;
  }
  .sa-item:hover { background: rgba(255,255,255,0.06); color: #fbbf24; }
  .sa-icon {
    width: 1rem;
    font-size: 0.7rem;
  }
  .sa-toast {
    position: absolute;
    top: calc(100% + 0.2rem);
    right: 0;
    padding: 0.18rem 0.45rem;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    white-space: nowrap;
    z-index: 99;
    pointer-events: none;
  }
  .sa-toast.ok {
    background: rgba(74,222,128,0.18);
    color: #4ade80;
    border: 1px solid rgba(74,222,128,0.45);
  }
  .sa-toast.err {
    background: rgba(248,113,113,0.18);
    color: #f87171;
    border: 1px solid rgba(248,113,113,0.45);
  }
</style>
