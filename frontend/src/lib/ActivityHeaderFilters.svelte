<!--
  ActivityHeaderFilters — header strip shared by every activity
  surface (ActivityLogModal, /orders Activity card, /activity page).
  Hosts the canonical filter chrome: account multiselect + log
  level dropdown.

  Operator: "the account drop down should filter the messages based
  on the account... the header should also have drop down for all,
  error, warning, info log type. the default is error... when active
  tab changes, it should apply to the new active tab."

  Both filters are bindable so the parent owns the state and threads
  them to ActivityLogSurface (which threads them to LogPanel). When
  the active tab changes inside LogPanel, the filters keep applying
  to whichever tab is now visible — single source of truth, no
  per-tab state reset.

  Usage:
    <ActivityHeaderFilters
      bind:accountFilter={_accountFilter}
      bind:levelFilter={_levelFilter}
      availableAccounts={_availableAccounts} />
-->
<script>
  import ActivityAccountSelect from '$lib/ActivityAccountSelect.svelte';

  let {
    /** Selected account codes — bindable. */
    accountFilter     = $bindable(/** @type {string[]} */ ([])),
    /** Active log-level filter — bindable. Default 'all' (operator
     *  reverted earlier 'error' default to 'all' so the conn / system
     *  tabs show their INFO paper trail by default). */
    levelFilter       = $bindable(/** @type {'all'|'error'|'warning'|'info'} */ ('all')),
    /** Account codes present in the currently-loaded rows. */
    availableAccounts = /** @type {string[]} */ ([]),
  } = $props();

  // Stable label list — operator-visible. 'All' first so the operator
  // can quickly drop the filter; 'Error' is the default landing.
  const _LEVELS = /** @type {Array<{value:'all'|'error'|'warning'|'info', label:string}>} */ ([
    { value: 'all',     label: 'All'     },
    { value: 'error',   label: 'Error'   },
    { value: 'warning', label: 'Warning' },
    { value: 'info',    label: 'Info'    },
  ]);
</script>

<span class="act-filters">
  <ActivityAccountSelect
    bind:value={accountFilter}
    {availableAccounts} />
  <label class="act-level" title="Filter rows by log level (applies to the active tab)">
    <span class="act-level-glyph" aria-hidden="true">⚑</span>
    <select bind:value={levelFilter} class="act-level-sel"
            aria-label="Log level filter">
      {#each _LEVELS as L}
        <option value={L.value}>{L.label}</option>
      {/each}
    </select>
  </label>
</span>

<style>
  /* Flex group so both controls land together — parent decides the
     overall placement (margin-left:auto on the wrapper if needed). */
  .act-filters {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    margin-left: auto;
  }
  .act-level {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    font-size: 0.7rem;
    color: rgba(255, 255, 255, 0.72);
  }
  .act-level-glyph {
    font-size: 0.78rem;
    color: rgba(251, 191, 36, 0.85);
    line-height: 1;
  }
  .act-level-sel {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 3px;
    color: rgba(255, 255, 255, 0.85);
    font-size: 0.68rem;
    padding: 0.15rem 0.35rem;
    line-height: 1.15;
    cursor: pointer;
  }
  .act-level-sel:hover {
    border-color: rgba(255, 255, 255, 0.22);
  }
  .act-level-sel:focus-visible {
    outline: 2px solid rgba(34, 211, 238, 0.6);
    outline-offset: 1px;
  }
</style>
