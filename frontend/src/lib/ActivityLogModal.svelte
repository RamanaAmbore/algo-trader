<script>
  /**
   * ActivityLogModal — modal wrapper around <LogPanel>.
   *
   * Renders the canonical 5-tab activity surface (Order Book · Agent Log ·
   * Terminal · System · News) — same component the orders page and console
   * surface inline. Reusing LogPanel means the tab strip, rotated "log"
   * vertical label, kind-coloured row accents, and 4Hz polling all
   * behave identically wherever the operator opens the activity surface.
   *
   * Modal frame matches ChartModal + SymbolPanel modal-mode: viewport-
   * centred via .canonical-modal-overlay / .canonical-modal-panel (defined
   * in app.css), pointer-events-none overlay so the page underneath
   * stays clickable while activity is open. Esc and × button close.
   */

  import { onMount, onDestroy } from 'svelte';
  import LogPanel from '$lib/LogPanel.svelte';
  import { portal } from '$lib/portal';

  let {
    /** @type {() => void} */
    onClose,
  } = $props();

  let _modalEl = $state(/** @type {HTMLElement|null} */ (null));
  let _closeBtnEl = $state(/** @type {HTMLButtonElement|null} */ (null));

  function _focusables() {
    return /** @type {HTMLElement[]} */ (
      Array.from(_modalEl?.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      ) ?? [])
    );
  }

  function _onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') {
      // Same capture-phase + stopImmediatePropagation pattern ChartModal
      // uses so the top-of-stack modal closes on Esc without cascading
      // into a parent modal.
      e.stopImmediatePropagation();
      onClose?.();
      return;
    }
    if (e.key === 'Tab') {
      const els = _focusables();
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
  }

  function _onCloseClick(/** @type {MouseEvent} */ e) {
    e.stopPropagation();
    onClose?.();
  }

  onMount(() => {
    window.addEventListener('keydown', _onKey, { capture: true });
    _closeBtnEl?.addEventListener('click', _onCloseClick);
    setTimeout(() => { _focusables()[0]?.focus(); }, 0);
  });
  onDestroy(() => {
    window.removeEventListener('keydown', _onKey, { capture: true });
    _closeBtnEl?.removeEventListener('click', _onCloseClick);
  });
</script>

<div class="canonical-modal-overlay alm-overlay" use:portal role="dialog" aria-modal="true"
     aria-label="Activity log" tabindex="-1">
  <div class="canonical-modal-panel alm-panel" bind:this={_modalEl}>
    <!-- Modal chrome — title + close button. Tabs live inside LogPanel
         so the tab strip is consistent with every other LogPanel mount. -->
    <div class="alm-header">
      <span class="alm-title">Activity</span>
      <button type="button" class="alm-close" bind:this={_closeBtnEl}
              aria-label="Close activity log">×</button>
    </div>

    <div class="alm-body">
      <LogPanel
        heightClass="flex-1 min-h-0"
        defaultTab="order"
        tabs={['order','agent','terminal','system','news']}
      />
    </div>
  </div>
</div>

<style>
  .alm-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid rgba(168, 85, 247, 0.22);
    flex-shrink: 0;
  }
  .alm-title {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: #c4b5fd;
  }
  .alm-close {
    margin-left: auto;
    width: 1.8rem;
    height: 1.8rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    border-radius: 3px;
    color: #f87171;
    font-size: 1.1rem;
    line-height: 1;
    padding: 0;
    cursor: pointer;
    font-family: monospace;
    transition: background 0.1s;
    pointer-events: auto;
    position: relative;
    z-index: 2;
    flex-shrink: 0;
  }
  .alm-close:hover {
    background: rgba(248, 113, 113, 0.15);
  }

  .alm-body {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    padding: 0.4rem 0.6rem 0.6rem;
    overflow: hidden;
  }
  /* Panel-specific border tint — violet matches the Log icon's palette
     in PageHeaderActions so the operator gets a visual "this is the
     activity surface" signal. The frame size + position is canonical. */
  :global(.canonical-modal-panel.alm-panel) {
    border-color: rgba(168, 85, 247, 0.40);
  }
</style>
