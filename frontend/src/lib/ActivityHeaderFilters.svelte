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
  <!-- Level filter — chrome matches MultiSelect (amber-on-slate gradient,
       same border/radius/font/height) so the two controls in the header
       read as a single visual pair. Operator: "the two dropdowns in
       activity header are not aligned, dont have consistent colour
       scheme." -->
  <select bind:value={levelFilter} class="act-level-sel"
          aria-label="Log level filter"
          title="Filter rows by log level (applies to the active tab)">
    {#each _LEVELS as L}
      <option value={L.value}>{L.label}</option>
    {/each}
  </select>
</span>

<style>
  /* Flex group so both controls land together — parent decides the
     overall placement (margin-left:auto on the wrapper if needed).
     align-items: stretch so both children share the same height. */
  .act-filters {
    display: inline-flex;
    align-items: stretch;
    gap: 0.4rem;
    margin-left: auto;
  }
  /* Level dropdown chrome — exact match to MultiSelect's trigger
     (frontend/src/lib/MultiSelect.svelte .rbq-multi-trigger). Same
     gradient bg, amber border, slate text, font-size, padding,
     min-height, border-radius. Keep these in lockstep when the
     MultiSelect chrome changes. */
  .act-level-sel {
    min-height: 1.55rem;
    padding: 0.25rem 1.4rem 0.25rem 0.5rem;
    background-image: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    background-color: #1d2a44;
    border: 1px solid rgba(251, 191, 36, 0.25);
    border-radius: 3px;
    color: var(--algo-slate);
    font-size: 0.62rem;
    font-family: inherit;
    cursor: pointer;
    line-height: 1.15;
    /* Native caret repositioned to align with MultiSelect's amber caret
       — same 0.95rem visual weight, same right-edge gap. */
    -webkit-appearance: none;
    -moz-appearance: none;
    appearance: none;
    background-image:
      linear-gradient(180deg, #273552 0%, #1d2a44 100%),
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath fill='%23fbbf24' d='M5 6 0 0h10z'/%3E%3C/svg%3E");
    background-repeat: no-repeat, no-repeat;
    background-position: center, right 0.45rem center;
    background-size: auto, 0.5rem 0.3rem;
    transition: border-color 0.08s;
  }
  .act-level-sel:hover         { border-color: rgba(251, 191, 36, 0.6); }
  .act-level-sel:focus         { outline: none; border-color: #fbbf24; }
  .act-level-sel:focus-visible { outline: none; border-color: #fbbf24; }
  /* Options inherit dark bg from the page; spell out the contrast so
     OS-dark-mode users don't see washed-out white-on-white menus. */
  .act-level-sel option {
    background: #1d2a44;
    color: var(--algo-slate);
  }
</style>
