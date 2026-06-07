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
   *   tabs: Array<{ id: string, label: string, badge?: number|string, color?: 'amber'|'cyan'|'green'|'sky'|'rose' }>,
   *   value: string,
   *   onChange?: (id: string) => void,
   *   compact?: boolean,
   * }} */
  let { tabs, value = $bindable(), onChange, compact = false } = $props();

  function select(id) {
    value = id;
    onChange?.(id);
  }
</script>

<div role="tablist" class="algo-tabs-strip">
  {#each tabs as tab (tab.id)}
    {@const color = tab.color ?? 'amber'}
    <button
      type="button"
      role="tab"
      class="algo-tab algo-tab-c-{color}"
      class:algo-tab--compact={compact}
      aria-selected={value === tab.id}
      onclick={() => select(tab.id)}
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
    display: inline-flex;
    gap: 0;
    align-items: stretch;
  }
</style>
