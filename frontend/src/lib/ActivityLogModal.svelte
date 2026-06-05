<script>
  /**
   * ActivityLogModal — modal wrapper around <LogPanel>.
   *
   * Renders the canonical 6-tab activity surface (Orders · Agents ·
   * Terminal · Ticks · System · News) — same component the orders page and console
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
      <!-- Leading icon mirrors the page-header Activity button glyph
           (three horizontal lines) so the operator recognises which
           surface they opened. Violet matches the bell palette. -->
      <span class="alm-title">
        <!-- Notification bell glyph — matches the page-header Activity
             button and the LogPanel section icon. Bell semantics
             align with the existing notifications family. -->
        <svg class="alm-title-icon" width="12" height="12" viewBox="0 0 16 16"
             fill="none" stroke="currentColor" stroke-width="1.4"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M8 2c-2.4 0-4 1.9-4 4.2 0 2.1-.8 3.6-1.7 4.5-.3.3-.1.8.3.8h10.8c.4 0 .6-.5.3-.8-.9-.9-1.7-2.4-1.7-4.5C12 3.9 10.4 2 8 2z" />
          <path d="M6.6 13c.2.8.8 1.3 1.4 1.3.6 0 1.2-.5 1.4-1.3" />
        </svg>
        Activity
      </span>
      <button type="button" class="alm-close" bind:this={_closeBtnEl}
              aria-label="Close activity log">×</button>
    </div>

    <div class="alm-body">
      <!-- Tab list inherited from LogPanel's default — keeps every
           surface (this modal, the Order modal bottom panel, /console,
           /agents) in sync without duplicating the array per callsite. -->
      <LogPanel
        heightClass="flex-1 min-h-0"
        defaultTab="order"
      />
    </div>
  </div>
</div>

<style>
  .alm-header {
    /* Standard modal header — reduced vertical padding (0.35rem)
       matches ChartModal + SymbolPanel so all three modals open
       with the same chrome height. */
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.85rem;
    background: linear-gradient(180deg,
                  rgba(168, 85, 247, 0.12) 0%,
                  rgba(168, 85, 247, 0.04) 100%);
    border-bottom: 1px solid rgba(168, 85, 247, 0.55);
    flex-shrink: 0;
  }
  /* Matches .cm-title / .oes-modal-name typography on the sibling
     modals — natural-case, weight 600, no uppercase / letter-spacing.
     Earlier styling rendered the label as "LOG" caps which was out
     of sync with the rest of the modal trio. */
  .alm-title {
    /* Standard 0.72rem matches the sibling modal titles
       (ChartModal / SymbolPanel). */
    font-family: monospace;
    font-size: 0.72rem;
    color: #c4b5fd;
    font-weight: 700;
    letter-spacing: 0.04em;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Matches the page-header Activity button's resting colour
     (#a855f7 = violet-500) so the modal title icon is the exact same
     shade as the button that opened it. */
  .alm-title-icon { color: #a855f7; flex-shrink: 0; }
  .alm-close {
    /* Standard close — square 1.4rem matches ChartModal +
       SymbolPanel close buttons; glyph 0.95rem is proportional to
       the 0.72rem header title text. */
    margin-left: auto;
    width: 1.4rem;
    height: 1.4rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    border-radius: 3px;
    color: #f87171;
    font-size: 0.95rem;
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
