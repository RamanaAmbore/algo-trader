<script>
  // ChartModal — thin overlay wrapper around ChartWorkspace.
  // Opens as a fixed-inset modal with an amber-glow navy panel.
  // Esc key and overlay click both close.

  import { onMount, onDestroy } from 'svelte';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import { portal } from '$lib/portal';

  let {
    /** @type {string} */ symbol = '',
    /** @type {string} */ exchange = '',
    /** @type {'live'|'sim'|'paper'} */ mode = 'live',
    /** @type {() => void} */ onClose,
  } = $props();

  let _modalEl = $state(/** @type {HTMLElement|null} */ (null));
  let _closeBtnEl = $state(/** @type {HTMLButtonElement|null} */ (null));

  // The cm-overlay is portaled to document.body, OUTSIDE the SvelteKit
  // mount root (<div id="svelte">). Svelte 5 delegates onclick handlers
  // at the mount root, so any onclick={...} on nodes inside the portal
  // never fires — the click bubbles up to <body> and stops there.
  // Bind the X-button click natively via addEventListener instead.
  function _onCloseClick(/** @type {MouseEvent} */ e) {
    e.stopPropagation();
    onClose?.();
  }

  function _focusables() {
    return /** @type {NodeListOf<HTMLElement>} */ (
      _modalEl?.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])') ?? []
    );
  }

  function _onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') {
      // stopImmediatePropagation prevents the parent SymbolPanel's
      // capture-phase listener from also firing and closing it when
      // ChartModal is the top-of-stack modal.
      e.stopImmediatePropagation();
      onClose?.();
      return;
    }
    if (e.key === 'Tab') {
      const els = Array.from(_focusables());
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
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

<!-- svelte-ignore a11y_interactive_supports_focus -->
<!-- overlay is pointer-events:none so click-outside-to-close is gone;
     operator uses × button or Esc. tabindex retained for screen readers. -->
<div class="canonical-modal-overlay cm-overlay" use:portal role="dialog" aria-modal="true"
     aria-label="Chart — {symbol}" tabindex="-1">
  <div class="canonical-modal-panel cm-modal" bind:this={_modalEl}>
    <div class="cm-header">
      <span class="cm-title">Chart — <span class="cm-sym">{symbol}</span></span>
      <button type="button" class="cm-close" bind:this={_closeBtnEl}
              aria-label="Close chart modal">×</button>
    </div>
    <div class="cm-body">
      <ChartWorkspace
        symbol={symbol}
        exchange={exchange}
        mode={mode}
        compact={false}
        showHeader={false}
      />
    </div>
  </div>
</div>

<style>
  /* Frame (overlay + panel positioning, size, border, gradient) lives in
     app.css under .canonical-modal-overlay / .canonical-modal-panel.
     Local styles only carry the chart-specific header + close-button
     chrome below. */

  .cm-header {
    display: flex;
    align-items: center;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .cm-title {
    font-family: monospace;
    font-size: 0.65rem;
    color: #7e97b8;
    font-weight: 600;
  }

  .cm-sym {
    color: #7dd3fc;
    font-weight: 700;
  }

  .cm-close {
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
    /* Defensive: ensure the button always receives clicks regardless
       of any ancestor's pointer-events setting and that nothing inside
       the chart workspace can paint over it. */
    pointer-events: auto;
    position: relative;
    z-index: 2;
    flex-shrink: 0;
  }
  .cm-close:hover {
    background: rgba(248, 113, 113, 0.15);
  }

  .cm-body {
    overflow: hidden;
    flex: 1 1 0;
    min-height: 0;
  }
</style>
