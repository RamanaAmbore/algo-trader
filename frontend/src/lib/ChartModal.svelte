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
  // ChartWorkspace exposes its fetch state via $bindable `loading`. While
  // a refresh is in flight, the modal goes into a "busy" guard mode:
  //   - overlay flips to pointer-events:auto so navbar / menu clicks
  //     underneath are absorbed instead of triggering navigation
  //   - chart body becomes pointer-events:none so hover/zoom are inert
  //   - only the × button + Esc key still close the modal
  // Mirrors industry pattern (TWS dialog modal lock, Bloomberg busy
  // cursor) — when data is fetching, no surface clicks are honoured so
  // the operator can't accidentally double-trigger.
  let _loading = $state(false);

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
<div class="canonical-modal-overlay cm-overlay" class:cm-busy={_loading}
     use:portal role="dialog" aria-modal="true"
     aria-label="Chart — {symbol}" tabindex="-1">
  <div class="canonical-modal-panel cm-modal" class:cm-busy={_loading} bind:this={_modalEl}>
    <div class="cm-header">
      <!-- Modal-name only — the symbol picker lives inside ChartWorkspace,
           and showing the symbol up here too would duplicate it.
           Plural matches the /charts page route name. The leading icon
           mirrors the page-header Chart button glyph so the operator
           reads "I'm in Charts" at a glance. -->
      <span class="cm-title">
        <svg class="cm-title-icon" width="12" height="12" viewBox="0 0 16 16"
             fill="none" stroke="currentColor" stroke-width="1.9"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M2 13h12M3 11l3-4 3 2 4-6" />
        </svg>
        Charts
        {#if _loading}
          <!-- Busy badge — rotating arc-spinner glyph in chart-icon
               cyan. Tells the operator "refresh in flight, modal locked
               to × only". No text label; the rotation + cyan tint is
               the affordance. -->
          <span class="cm-busy-badge" aria-live="polite" title="Refreshing chart — modal is locked until done">
            <svg class="cm-busy-badge-icon" width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <circle cx="8" cy="8" r="5.5"
                fill="none" stroke="currentColor" stroke-width="2"
                stroke-linecap="round"
                stroke-dasharray="9 30" />
            </svg>
          </span>
        {/if}
      </span>
      <button type="button" class="cm-close" bind:this={_closeBtnEl}
              aria-label="Close chart modal">×</button>
    </div>
    <div class="cm-body">
      <ChartWorkspace
        bind:loading={_loading}
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
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Matches the page-header Chart button's resting colour (#22d3ee
     = cyan-400) so the modal title icon is the exact same shade as
     the button that opened it. */
  .cm-title-icon { color: #22d3ee; flex-shrink: 0; }

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

  /* Busy state — chart fetch in flight. */
  /* Overlay swallows clicks on the page underneath (menu / navbar /
     content) so the operator can't trigger a navigation that races
     the in-flight fetch. */
  .cm-overlay.cm-busy { pointer-events: auto; }
  /* Inside the panel, header text is read-only; the chart body becomes
     inert so hover/zoom/pan/scroll don't fire during the fetch. The ×
     button retains pointer-events:auto (set on .cm-close directly) and
     stays clickable. */
  .cm-modal.cm-busy .cm-body { pointer-events: none; }
  /* Subtle scrim over the body so the lock state is visually obvious. */
  .cm-modal.cm-busy .cm-body::after {
    content: '';
    position: absolute;
    inset: 0;
    background: rgba(13, 24, 41, 0.18);
    pointer-events: none;
  }
  .cm-body { position: relative; }

  /* Refreshing badge in the header — icon-only, cyan to match the
     canonical chart-icon palette across the page-header Chart button
     and the in-chart spinner. No background pill or text label; the
     rotation alone communicates the state, leaving the header
     uncluttered. */
  .cm-busy-badge {
    display: inline-flex;
    align-items: center;
    margin-left: 0.4rem;
    color: #22d3ee;
  }
  .cm-busy-badge-icon {
    animation: cm-busy-spin 1.1s linear infinite;
    transform-origin: 50% 50%;
  }
  @keyframes cm-busy-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
</style>
