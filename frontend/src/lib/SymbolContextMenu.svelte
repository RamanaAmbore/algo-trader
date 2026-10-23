<script>
  // SymbolContextMenu — the SINGLE shared floating context menu for symbol
  // cells across the whole app (2026-10-22 unification — previously
  // MarketPulse.svelte hand-rolled its own separate menu; see that file's
  // history for the 10-item version this replaces).
  // Triggered by right-click (desktop) or long-press (mobile).
  //
  // The menu itself renders here; sub-modals (SymbolPanel, ChartModal,
  // ActivityLogModal) are mounted alongside in the parent so they survive
  // after the menu closes. The parent drives this via the `onAction` prop.
  //
  // Base actions, emitted on every host regardless of props (the
  // original 5-item menu; every host still gets this set unconditionally,
  // in addition to whichever opt-in items below its data supports):
  //   place-order  — parent should open SymbolPanel
  //   close        — parent should open SymbolPanel pre-seeded with the
  //                  reverse-side / close-position context (slice AV
  //                  audit fix — most time-critical action used to need
  //                  3+ clicks; now one right-click). Only emitted when
  //                  `currentQty` is non-zero and `isDraft` is false.
  //   chart        — parent should open ChartModal
  //   orders       — navigate to /orders?symbol=<sym>
  //   log          — parent should open ActivityLogModal
  //
  // Opt-in actions (only rendered when the matching prop is passed /
  // the underlying data is actually available for that row — gated on
  // DATA, not on which page is rendering it; 2026-10-22 parity pass made
  // all four available from every host that mounts this menu —
  // MarketPulse, OrderBook, LogPanel, PerformancePage, the derivatives
  // Legs grid — each host decides per-row whether the prop applies):
  //   open-options     — showOpenInOptions=true — jump to the
  //                      derivatives page for this row's underlying root.
  //                      Every host passes true except when the root
  //                      already matches the page's own selected
  //                      underlying (the derivatives page itself —
  //                      otherwise genuinely redundant, same page).
  //   copy-symbol      — showCopySymbol=true. Needs only the symbol
  //                      string, universal everywhere.
  //   detach / reattach — group={isDetached, hasUnderlying} (underlying-
  //                      root visual clustering override — the override
  //                      itself only visibly affects MarketPulse's own
  //                      grid, but is wired from every host so an
  //                      operator can detach a symbol while looking at
  //                      it on any surface; `hasUnderlying` gates on
  //                      whether the symbol parses as an option/future).
  //   add-watchlist / remove-watchlist — watchlist={inWatchlist}. Gated
  //                      on membership data being loaded/available for
  //                      that symbol; null hides both items.
  //   select-draft / promote — isDraft=true. Replaces the base
  //                      place-order/close pair entirely (a draft/what-if
  //                      row has nothing real to close) and suppresses
  //                      every opt-in item above — the draft menu is
  //                      deliberately a small, focused set: Promote to
  //                      real, Select for clear, Chart, Orders, Log.
  //
  // Every action is also gated on `canPlaceOrders` (default true) for the
  // order-placing items (place-order / close / promote / select-draft) —
  // mirrors the host page's own "does a plain click open the ticket"
  // gate (e.g. MarketPulse's `allowOrders`). Chart / Orders / Log / Copy
  // symbol / watchlist / group items are informational, not order
  // placement, and stay available regardless.
  //
  // Closes on: click-outside, Esc, any item click.

  import { onMount, onDestroy } from 'svelte';
  import { portal } from '$lib/portal';
  import { goto } from '$app/navigation';
  import LegLabel from '$lib/LegLabel.svelte';

  /** @type {{
   *   symbol: string,
   *   exchange?: string,
   *   x: number,
   *   y: number,
   *   currentQty?: number,
   *   isDraft?: boolean,
   *   canPlaceOrders?: boolean,
   *   showOpenInOptions?: boolean,
   *   showCopySymbol?: boolean,
   *   watchlist?: { inWatchlist: boolean } | null,
   *   group?: { isDetached: boolean, hasUnderlying?: boolean } | null,
   *   onClose: () => void,
   *   onAction?: (action: string, symbol: string, exchange: string) => void,
   * }} */
  let {
    symbol     = '',
    exchange   = '',
    x          = 0,
    y          = 0,
    /** Pass the row's signed qty so the menu can show "Close" only on
     * rows that actually have an open position. Pages that don't track
     * per-row qty (e.g. a watchlist click) can leave this at 0 — the
     * Close item simply doesn't render. */
    currentQty = 0,
    /** Draft (what-if, payoff-store) row — never has a real broker
     * position to close. Swaps place-order/close for Promote-to-real +
     * Select-for-clear and suppresses every other opt-in item. */
    isDraft    = false,
    /** Gate for place-order / close / promote / select-draft — mirrors
     * the host's own "does clicking this row place an order" gate. */
    canPlaceOrders = true,
    /** Jump to the derivatives page for this row's underlying root. Any
     * host may pass true — gate on data (e.g. suppress when the root
     * already matches the page's own selected underlying), not on
     * which page is rendering. */
    showOpenInOptions = false,
    /** "Copy symbol" item. Needs only the symbol string — safe to pass
     * true unconditionally from any host. */
    showCopySymbol = false,
    /** Watchlist add/remove. Omit (null) to hide both — e.g. while
     * membership is still loading, or the row has no resolvable symbol. */
    watchlist = /** @type {{ inWatchlist: boolean } | null} */ (null),
    /** Underlying-root group attach/detach (Pulse's grouping override).
     * Omit (null) to hide both. `hasUnderlying` only matters when
     * `isDetached` is false — a detached symbol always gets the
     * Re-attach escape hatch regardless, since that state is otherwise
     * only reversible via clearing localStorage. */
    group = /** @type {{ isDetached: boolean, hasUnderlying?: boolean } | null} */ (null),
    onClose,
    onAction = null,
  } = $props();

  // Direction label for the Close menu item — long positions buy back
  // via SELL, short positions buy back via BUY. Operator's mental model
  // is "what side do I press to flatten this row".
  const _closeLabel = $derived(
    Number(currentQty) > 0 ? 'Close (sell)' :
    Number(currentQty) < 0 ? 'Close (buy)'  : ''
  );

  /** @type {HTMLElement | null} */
  let menuEl = $state(null);

  // Clamp the menu so it doesn't escape the viewport. The estimate below
  // is generous for the largest (MarketPulse, non-draft) item set —
  // better to leave a little dead space on a short menu than clip a long
  // one off-screen.
  const _clampedX = $derived(Math.min(x, (typeof window !== 'undefined' ? window.innerWidth  : 800) - 190));
  const _clampedY = $derived(Math.min(y, (typeof window !== 'undefined' ? window.innerHeight : 600) - 280));

  function _fire(action) {
    // onAction BEFORE onClose (2026-10-22 fix) — every host's onClose
    // nulls its own `_ctxMenu`/`ctxMenu` state, which several onAction
    // handlers read from (e.g. `/orders`' `_ctxMenu?.currentQty`). The
    // old order (onClose then onAction) meant that read always saw null,
    // so `/orders`' Close flow always opened with currentQty=0 — a
    // latent real-money bug (wrong side pre-filled) this reorder fixes
    // for every host, not just the new draft/row-type wiring below that
    // depends on the same pattern.
    if (onAction) {
      onAction(action, symbol, exchange);
    } else if (action === 'orders') {
      goto(`/orders?symbol=${encodeURIComponent(symbol.toUpperCase())}`);
    }
    onClose();
  }

  // Close on Esc
  function _onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') onClose();
  }
  // Close on outside click — defer by one tick so the triggering
  // mouseup doesn't immediately re-fire this handler.
  function _onDocClick(/** @type {MouseEvent} */ e) {
    if (menuEl && !menuEl.contains(/** @type {Node} */ (e.target))) {
      onClose();
    }
  }

  onMount(() => {
    window.addEventListener('keydown',  _onKey);
    setTimeout(() => {
      window.addEventListener('click', _onDocClick);
    }, 0);
  });
  onDestroy(() => {
    window.removeEventListener('keydown',  _onKey);
    window.removeEventListener('click',    _onDocClick);
  });
</script>

<!-- svelte-ignore a11y_interactive_supports_focus -->
<div
  bind:this={menuEl}
  use:portal
  class="ctx-menu scm-menu"
  style="left:{_clampedX}px;top:{_clampedY}px"
  role="menu"
  aria-label="Symbol actions for {symbol}">

  <div class="scm-header"><LegLabel sym={symbol} /></div>
  <div class="ctx-sep"></div>

  {#if isDraft}
    {#if canPlaceOrders}
      <button class="ctx-item" role="menuitem" data-action="promote" onclick={() => _fire('promote')}>
        Promote to real
      </button>
      <button class="ctx-item" role="menuitem" data-action="select-draft" onclick={() => _fire('select-draft')}>
        Select for clear
      </button>
      <div class="ctx-sep"></div>
    {/if}
    <button class="ctx-item" role="menuitem" data-action="chart" onclick={() => _fire('chart')}>
      Chart
    </button>
    <button class="ctx-item" role="menuitem" data-action="orders" onclick={() => _fire('orders')}>
      Orders
    </button>
    <button class="ctx-item" role="menuitem" data-action="log" onclick={() => _fire('log')}>
      Log
    </button>
  {:else}
    {#if canPlaceOrders}
      <button class="ctx-item" role="menuitem" data-action="place-order" onclick={() => _fire('place-order')}>
        Place order
      </button>
      {#if _closeLabel}
        <button class="ctx-item ctx-item-danger" role="menuitem" data-action="close"
                onclick={() => _fire('close')}>
          {_closeLabel}
        </button>
      {/if}
    {/if}
    {#if showOpenInOptions}
      <button class="ctx-item" role="menuitem" data-action="open-options" onclick={() => _fire('open-options')}>
        Open in Options
      </button>
    {/if}
    <button class="ctx-item" role="menuitem" data-action="chart" onclick={() => _fire('chart')}>
      Chart
    </button>
    <button class="ctx-item" role="menuitem" data-action="orders" onclick={() => _fire('orders')}>
      Orders
    </button>
    <button class="ctx-item" role="menuitem" data-action="log" onclick={() => _fire('log')}>
      Log
    </button>
    {#if showCopySymbol}
      <div class="ctx-sep"></div>
      <button class="ctx-item" role="menuitem" data-action="copy-symbol" onclick={() => _fire('copy-symbol')}>
        Copy symbol
      </button>
    {/if}
    {#if group}
      <div class="ctx-sep"></div>
      {#if group.isDetached}
        <button class="ctx-item" role="menuitem" data-action="reattach" onclick={() => _fire('reattach')}>
          ↩ Re-attach to group
        </button>
      {:else if group.hasUnderlying}
        <button class="ctx-item" role="menuitem" data-action="detach" onclick={() => _fire('detach')}>
          ↗ Detach from group
        </button>
      {/if}
    {/if}
    {#if watchlist}
      <div class="ctx-sep"></div>
      {#if watchlist.inWatchlist}
        <button class="ctx-item ctx-item-danger" role="menuitem" data-action="remove-watchlist"
                onclick={() => _fire('remove-watchlist')}>
          Remove from watchlist
        </button>
      {:else}
        <button class="ctx-item" role="menuitem" data-action="add-watchlist" onclick={() => _fire('add-watchlist')}>
          ★ Add to watchlist
        </button>
      {/if}
    {/if}
  {/if}
</div>

<style>
  /* Canonical popup chrome for every right-click/long-press symbol menu
     app-wide. Relocated here from MarketPulse.svelte (2026-10-22 context-
     menu unification) — this component is now the ONLY renderer of
     `.ctx-menu`/`.ctx-item`/`.ctx-sep` markup; MarketPulse's own
     hand-rolled menu (and its duplicate, rgba-literal `.ctx-item-danger`
     variant) is gone, so these styles have one owner instead of living
     in a component that no longer uses them. Kept as `:global()` since
     nothing else in the markup above needs component-scoped specificity. */
  :global(.ctx-menu) {
    position: fixed;
    z-index: var(--z-tooltip);
    min-width: 10rem;
    background: rgba(10,22,40,0.97);
    border: 1px solid rgba(251,191,36,0.55);
    border-radius: 6px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.55);
    padding: 0.25rem 0;
    font-size: var(--fs-md);
    max-height: 80vh;
    overflow-y: auto;
  }
  :global(.ctx-item) {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.3rem 0.75rem;
    background: transparent;
    border: none;
    color: color-mix(in srgb, var(--algo-slate) 85%, transparent);
    cursor: pointer;
    font-size: var(--fs-md);
    white-space: nowrap;
    transition: background 0.1s, color 0.1s;
  }
  :global(.ctx-item:hover) {
    background: rgba(251,191,36,0.1);
    color: var(--c-action);
  }
  :global(.ctx-item-danger) { color: var(--c-short); }
  :global(.ctx-item-danger:hover) { background: var(--c-short-10); }
  :global(.ctx-sep) {
    height: 1px;
    background: color-mix(in srgb, var(--algo-slate) 10%, transparent);
    margin: 0.2rem 0;
  }

  /* Symbol name header above the action items. */
  .scm-menu {
    min-width: 11rem;
  }
  .scm-header {
    padding: 0.25rem 0.75rem 0.15rem;
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--c-action);
    text-transform: uppercase;
    user-select: none;
  }
</style>
