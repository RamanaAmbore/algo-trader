<script>
  /**
   * AlgoTabs — canonical underline-on-active tab strip for all algo pages.
   *
   * Props:
   *   tabs     — ordered list of tab descriptors.
   *   value    — currently-active tab id (two-way bindable).
   *   onChange — called with the new id when the operator clicks a tab.
   *   compact  — if true, uses the smaller padding variant (.algo-tab--compact).
   *
   * Tab descriptor shape:
   *   { id: string, label: string, badge?: number|string, color?: 'amber'|'cyan'|'green'|'sky'|'rose' }
   *
   * Color defaults to 'amber' when not supplied (matches the historical
   * platform default across LogPanel / SymbolPanel / research page).
   */

  /** @type {{
   *   tabs: Array<{ id: string, label: string, badge?: number|string, color?: 'amber'|'cyan'|'green'|'sky'|'rose', disabled?: boolean, disabledTitle?: string }>,
   *   value: string,
   *   onChange?: (id: string) => void,
   *   compact?: boolean,
   * }} */
  let { tabs, value = $bindable(), onChange, compact = false } = $props();

  function select(id, isDisabled) {
    if (isDisabled) return;
    value = id;
    onChange?.(id);
  }
</script>

<div role="tablist" class="algo-tabs-strip">
  {#each tabs as tab (tab.id)}
    {@const color = tab.color ?? 'amber'}
    {@const isDisabled = !!tab.disabled}
    <button
      type="button"
      role="tab"
      class="algo-tab algo-tab-c-{color}"
      class:algo-tab--compact={compact}
      class:algo-tab--disabled={isDisabled}
      aria-selected={value === tab.id}
      aria-disabled={isDisabled || undefined}
      disabled={isDisabled}
      title={isDisabled ? (tab.disabledTitle || '') : null}
      onclick={() => select(tab.id, isDisabled)}
    >
      {tab.label}
      {#if tab.badge != null && tab.badge !== 0 && tab.badge !== ''}
        <span class="algo-tab-badge algo-tab-badge-c-{color}">{tab.badge}</span>
      {/if}
    </button>
  {/each}
</div>

<style>
  .algo-tabs-strip {
    display: flex;
    gap: 0;
    align-items: stretch;
    /* When the consumer drops the strip into a card narrower than the
       sum of tab widths (mobile, narrow sidebars), the strip used to
       blow out the card and push the page's horizontal scroll,
       cascading into the fixed navbar + footer. Contain to a self-
       managed horizontal scroll. Hide the scrollbar — the active-tab
       underline + tab edges make the swipe affordance obvious. */
    max-width: 100%;
    overflow-x: auto;
    overflow-y: hidden;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }
  .algo-tabs-strip::-webkit-scrollbar { display: none; }
  /* Disabled tab — dimmer, no underline, cursor signals non-interactive. */
  .algo-tab.algo-tab--disabled {
    opacity: 0.42;
    cursor: not-allowed;
    pointer-events: auto;     /* keep tooltip; disabled attribute blocks the click */
  }
  /* Every algo-tab is a flex child of the strip and must refuse to
     shrink — otherwise wide labels squash and become unreadable. The
     strip's overflow-x handles the case when the row genuinely
     doesn't fit. Targets every color variant via the global class. */
  :global(.algo-tabs-strip .algo-tab) {
    flex-shrink: 0;
    white-space: nowrap;
  }
</style>
