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
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import ChaseCard from '$lib/order/ChaseCard.svelte';
  import ModalShell from '$lib/ModalShell.svelte';
  import { selectedStrategyId, strategyOpenSymbols } from '$lib/stores';
  import { activityStore } from '$lib/data/activityStore.svelte.js';

  let {
    /** @type {() => void} */
    onClose,
  } = $props();

  let _modalEl = $state(/** @type {HTMLElement|null} */ (null));

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

  onMount(() => {
    window.addEventListener('keydown', _onKey, { capture: true });
    setTimeout(() => { _focusables()[0]?.focus(); }, 0);
  });
  onDestroy(() => {
    window.removeEventListener('keydown', _onKey, { capture: true });
  });
</script>

<ModalShell open={true} {onClose} zIndex={10500} dim={false} passthrough={true} clickOutside={false} ariaLabel="Activity log">
  <div class="canonical-modal-panel alm-panel" bind:this={_modalEl}>
    <div class="alm-body">
      <!-- ChaseCard — surfaces OPEN algo_orders across paper/live/shadow.
           idle-hide CSS in ChaseCard suppresses it when empty, so this
           is non-disruptive when there are no active chases. -->
      <ChaseCard pollMs={3000} compact={true} />
      <!-- ActivityLogSurface with label="Activity" so LogPanel renders
           its own tab-row header (amber gradient bg, BellIcon, filters,
           close button). The alm-header div is replaced by LogPanel's
           own header chrome — single source of truth, no duplication. -->
      <ActivityLogSurface
        defaultTab={activityStore.activeTab}
        context="modal"
        label="Activity"
        {onClose}
        symbolFilter={$selectedStrategyId == null ? null : $strategyOpenSymbols}
        bind:accountFilter={activityStore.accountFilter}
        bind:levelFilter={activityStore.levelFilter}
        onTabChange={(id) => { activityStore.activeTab = id; }} />
    </div>
  </div>
</ModalShell>

<style>
  .alm-body {
    flex: 1 1 0;
    min-height: 0;
    max-height: none;
    display: flex;
    flex-direction: column;
    padding: 0.4rem 0.6rem 0.6rem;
    overflow: hidden;
  }
  /* Panel-specific border tint — orange matches the Log icon's new
     palette in PageHeaderActions so the operator gets a visual
     "this is the activity surface" signal. The frame size +
     position is canonical. */
  :global(.canonical-modal-panel.alm-panel) {
    border-color: rgba(251, 146, 60, 0.45);
  }
</style>
