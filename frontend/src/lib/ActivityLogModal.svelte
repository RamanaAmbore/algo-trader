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
  import ActivityHeaderFilters from '$lib/ActivityHeaderFilters.svelte';
  import ChaseCard from '$lib/order/ChaseCard.svelte';
  import BellIcon from '$lib/icons/BellIcon.svelte';
  import ModalShell from '$lib/ModalShell.svelte';
  import { selectedStrategyId, strategyOpenSymbols } from '$lib/stores';
  import { activityStore } from '$lib/data/activityStore.svelte.js';

  let {
    /** @type {() => void} */
    onClose,
  } = $props();

  // availableAccounts is per-mount ephemeral — derived from the current
  // order rows by LogPanel. Not stored globally (different mounts have
  // different row sets; a stale value from a prior open would bleed
  // into the dropdown until the first poll completes).
  /** @type {string[]} */
  let _availableAccounts = $state([]);

  // Active tab mirrored from LogPanel via ActivityLogSurface's bindable.
  // Used to derive per-tab filter visibility in the modal header.
  let _activeTab = $state('');

  // Account filter: Orders, Agents, System, Conn tabs only.
  const _showAccountFilter = $derived(['order', 'agent', 'system', 'conn'].includes(_activeTab));
  // Level filter: Agents, System, Conn only (Orders has its own status filter).
  const _showLevelFilter   = $derived(['agent', 'system', 'conn'].includes(_activeTab));

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

<ModalShell open={true} {onClose} zIndex={10500} dim={false} passthrough={true} clickOutside={false} ariaLabel="Activity log">
  <div class="canonical-modal-panel alm-panel" bind:this={_modalEl}>
    <!-- Modal chrome — title + close button. Tabs live inside LogPanel
         so the tab strip is consistent with every other LogPanel mount. -->
    <div class="alm-header">
      <!-- Leading icon mirrors the page-header Activity button glyph
           (three horizontal lines) so the operator recognises which
           surface they opened. Violet matches the bell palette. -->
      <span class="alm-title">
        <!-- Shared BellIcon — flat orange (#fb923c), amber-700 (#b45309) outline.
             Replaces the inlined SVG with hard-coded #9a3412 that was duplicated
             in PageHeaderActions. Same bell identity on both surfaces. -->
        <BellIcon width="14" height="14" class="alm-title-icon alm-title-icon-3d" />
        Activity
      </span>
      <!-- Account + level filters lifted out of LogPanel's tab row.
           Single shared component — same chrome /orders Activity
           card uses, so all surfaces stay visually identical.
           accountFilter + levelFilter bound to activityStore so
           state persists across modal open/close and is shared with
           the /activity page. -->
      <ActivityHeaderFilters
        bind:accountFilter={activityStore.accountFilter}
        bind:levelFilter={activityStore.levelFilter}
        availableAccounts={_availableAccounts}
        showAccountFilter={_showAccountFilter}
        showLevelFilter={_showLevelFilter} />
      <button type="button" class="alm-close" bind:this={_closeBtnEl}
              aria-label="Close activity log">×</button>
    </div>

    <div class="alm-body">
      <!-- ChaseCard — surfaces OPEN algo_orders across paper/live/shadow.
           idle-hide CSS in ChaseCard suppresses it when empty, so this
           is non-disruptive when there are no active chases. -->
      <ChaseCard pollMs={3000} compact={true} />
      <!-- Tab list inherited from LogPanel's default — keeps every
           surface (this modal, the Order modal bottom panel, /console,
           /automation) in sync without duplicating the array per callsite. -->
      <!-- ActivityLogSurface is the SAME wrapper /orders renders inside
           its Activity card. Encapsulates the canonical config so the
           two surfaces can't drift on multiColumn / hideInline /
           bindable shape.
           defaultTab + onTabChange wire the active tab through to
           activityStore so tab selection persists across open/close
           and is shared with the /activity page. -->
      <ActivityLogSurface
        defaultTab={activityStore.activeTab}
        context="modal"
        symbolFilter={$selectedStrategyId == null ? null : $strategyOpenSymbols}
        bind:accountFilter={activityStore.accountFilter}
        bind:availableAccounts={_availableAccounts}
        bind:levelFilter={activityStore.levelFilter}
        bind:activeTab={_activeTab}
        onTabChange={(id) => { activityStore.activeTab = id; }} />
    </div>
  </div>
</ModalShell>

<style>
  .alm-header {
    /* Operator: "The line below modal headers is too prominent.
       Reduce its prominence. Instead, change background color of
       the header for modals." Stronger orange-tinted gradient bg
       acts as the visual separator from the body; bottom border is
       a hairline (1px low-alpha). */
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.85rem;
    background: linear-gradient(180deg,
                  rgba(251, 191, 36, 0.18) 0%,
                  rgba(251, 191, 36, 0.06) 100%);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-shrink: 0;
    /* Tight gap on mobile so the [title][filters][close] cluster
       fits without the level dropdown overlapping the close. Title
       is intentionally short ("Activity") leaving room for the two
       compact dropdowns on the same row even at 360px. Operator:
       "on mobile, there is more space on title. keep the drop
       downs on header without overlapping or pushing x button on
       modal on mobile." */
  }
  @media (max-width: 520px) {
    .alm-header { gap: 0.3rem; padding: 0.35rem 0.55rem; }
  }
  .alm-title {
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    color: var(--c-action);
    font-weight: 800;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Matches the page-header Activity button's new orange resting
     colour (#fb923c = orange-400) so the modal title icon is the
     exact same shade as the button that opened it. */
  :global(.alm-title-icon) { color: var(--c-action); flex-shrink: 0; }
  /* .alm-acct CSS moved into ActivityAccountSelect.svelte so the
     dropdown chrome is shared with the /orders Activity card via
     the .act-acct class on the canonical component. */
  .alm-close {
    /* Standard close — square 1.4rem matches ChartModal +
       SymbolPanel close buttons; glyph 0.95rem is proportional to
       the 0.72rem header title text.
       margin-left: auto claims the right edge of row 1 on every
       viewport. Desktop: filters ALSO have auto so they pack
       directly to the close's left (both consume right-edge
       space). Mobile: filters wrap to row 2 (flex-basis:100%
       below), so the auto on close pushes it to the row-1 right
       edge with empty space between title and close. */
    margin-left: auto;
    flex-shrink: 0;
    width: 1.4rem;
    height: 1.4rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    border-radius: 3px;
    color: var(--c-short);
    font-size: var(--fs-xl);
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
  /* Panel-specific border tint — orange matches the Log icon's new
     palette in PageHeaderActions so the operator gets a visual
     "this is the activity surface" signal. The frame size +
     position is canonical. */
  :global(.canonical-modal-panel.alm-panel) {
    border-color: rgba(251, 146, 60, 0.45);
  }
</style>
