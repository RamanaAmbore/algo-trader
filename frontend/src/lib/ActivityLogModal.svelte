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
        <!-- Notification bell — operator: "make the bell icon look like
             a little 3D". Filled body with a violet radial gradient
             (light top, deep bottom) for a rounded surface, a small
             white specular highlight at the upper left, a darker
             outline + a soft drop shadow so the bell pops off the
             chip-pill background as a tiny dimensional object. -->
        <svg class="alm-title-icon alm-title-icon-3d" width="14" height="14"
             viewBox="0 0 16 16" aria-hidden="true">
          <defs>
            <radialGradient id="alm-bell-body" cx="35%" cy="30%" r="75%">
              <stop offset="0%"  stop-color="#e9d5ff" />
              <stop offset="55%" stop-color="#a855f7" />
              <stop offset="100%" stop-color="#6b21a8" />
            </radialGradient>
            <filter id="alm-bell-shadow" x="-30%" y="-20%" width="160%" height="150%">
              <feDropShadow dx="0" dy="0.6" stdDeviation="0.6"
                            flood-color="#000" flood-opacity="0.55" />
            </filter>
          </defs>
          <g filter="url(#alm-bell-shadow)">
            <!-- Bell body -->
            <path d="M8 2c-2.4 0-4 1.9-4 4.2 0 2.1-.8 3.6-1.7 4.5-.3.3-.1.8.3.8h10.8c.4 0 .6-.5.3-.8-.9-.9-1.7-2.4-1.7-4.5C12 3.9 10.4 2 8 2z"
                  fill="url(#alm-bell-body)"
                  stroke="#4c1d95" stroke-width="0.6"
                  stroke-linejoin="round" />
            <!-- Clapper -->
            <path d="M6.6 13c.2.8.8 1.3 1.4 1.3.6 0 1.2-.5 1.4-1.3z"
                  fill="#7e22ce" stroke="#4c1d95" stroke-width="0.5"
                  stroke-linejoin="round" />
            <!-- Specular highlight (upper-left) -->
            <path d="M6.2 4.3c-.6.5-1 1.2-1.1 2"
                  fill="none" stroke="rgba(255,255,255,0.65)"
                  stroke-width="0.7" stroke-linecap="round" />
            <!-- Small bell-knob -->
            <circle cx="8" cy="2.1" r="0.7" fill="#c4b5fd"
                    stroke="#4c1d95" stroke-width="0.4" />
          </g>
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
    /* Operator: "make all the modal header look like orders header
       background decoration". Navy gradient + white inset highlight
       + violet bottom border so the ActivityLogModal strip matches
       the ChartModal + SymbolPanel chrome while keeping its violet
       identity. */
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.85rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06);
    border-bottom: 1.5px solid rgba(168, 85, 247, 0.55);
    flex-shrink: 0;
  }
  /* Plain title text — operator: "remove pill kind of decoration
     for modal header text". Bold uppercase violet glyphs on the
     navy gradient strip; the gradient itself is the prominence. */
  .alm-title {
    font-family: ui-monospace, monospace;
    font-size: 0.72rem;
    color: #c4b5fd;
    font-weight: 800;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Matches the page-header Activity button's resting colour
     (#a855f7 = violet-500) so the modal title icon is the exact same
     shade as the button that opened it. */
  .alm-title-icon { color: #a855f7; flex-shrink: 0; }
  /* 3D bell variant — colour comes from the inline SVG gradient, so
     the base `color` is irrelevant; small vertical lift so the bell
     reads as a dimensional pin sitting on the chip rather than text. */
  .alm-title-icon-3d { transform: translateY(-0.5px); }
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
