<script>
  // SymbolContextMenu — a small floating context menu for symbol cells.
  // Triggered by right-click (desktop) or long-press (mobile).
  // Reuses the .ctx-menu / .ctx-item / .ctx-sep global CSS from MarketPulse.
  //
  // The menu itself renders here; sub-modals (SymbolPanel, ChartModal,
  // ActivityLogModal) are mounted alongside in the parent so they survive
  // after the menu closes. The parent drives this via the `onAction` prop.
  //
  // Actions emitted:
  //   place-order  — parent should open SymbolPanel
  //   chart        — parent should open ChartModal
  //   close        — parent should open SymbolPanel pre-seeded with the
  //                  reverse-side / close-position context (slice AV
  //                  audit fix — most time-critical action used to need
  //                  3+ clicks; now one right-click)
  //   orders       — navigate to /orders?symbol=<sym>
  //   log          — parent should open ActivityLogModal
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

  // Clamp the menu so it doesn't escape the viewport.
  const _clampedX = $derived(Math.min(x, (typeof window !== 'undefined' ? window.innerWidth  : 800) - 190));
  const _clampedY = $derived(Math.min(y, (typeof window !== 'undefined' ? window.innerHeight : 600) - 170));

  function _fire(action) {
    onClose();
    if (onAction) {
      onAction(action, symbol, exchange);
    } else if (action === 'orders') {
      goto(`/orders?symbol=${encodeURIComponent(symbol.toUpperCase())}`);
    }
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

  <button class="ctx-item" role="menuitem" onclick={() => _fire('place-order')}>
    Place order
  </button>
  {#if _closeLabel}
    <button class="ctx-item ctx-item-danger" role="menuitem"
            onclick={() => _fire('close')}>
      {_closeLabel}
    </button>
  {/if}
  <button class="ctx-item" role="menuitem" onclick={() => _fire('chart')}>
    Chart
  </button>
  <button class="ctx-item" role="menuitem" onclick={() => _fire('orders')}>
    Orders
  </button>
  <button class="ctx-item" role="menuitem" onclick={() => _fire('log')}>
    Log
  </button>
</div>

<style>
  /* Symbol name header above the action items. */
  .scm-menu {
    min-width: 11rem;
  }
  .scm-header {
    padding: 0.25rem 0.75rem 0.15rem;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: #fbbf24;
    text-transform: uppercase;
    user-select: none;
  }
  /* Close-position action — red accent so the operator sees it as
     destructive vs the neutral place-order / chart / orders items. */
  :global(.ctx-item.ctx-item-danger) {
    color: #f87171;
  }
  :global(.ctx-item.ctx-item-danger:hover) {
    background: rgba(248, 113, 113, 0.10);
  }
</style>
