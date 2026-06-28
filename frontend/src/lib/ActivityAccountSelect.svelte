<!--
  ActivityAccountSelect — the AccountMultiSelect dropdown used in
  every activity-card header. Single canonical mount that both
  ActivityLogModal and the inline /orders Activity card import,
  so the dropdown's width, placeholder, and visibility rule stay
  consistent across the modal and the page surfaces.

  Renders nothing when the account list has ≤1 entry, so a
  single-account (or demo) layout doesn't show a useless dropdown.

  Usage:
    <ActivityAccountSelect
      bind:value={_accountFilter}
      availableAccounts={_availableAccounts} />
-->
<script>
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';

  let {
    /** Selected account codes — bindable so parent owns state. */
    value             = $bindable(/** @type {string[]} */ ([])),
    /** Account codes present in the currently-loaded order rows. */
    availableAccounts = /** @type {string[]} */ ([]),
  } = $props();
</script>

{#if availableAccounts.length > 1}
  <span class="act-acct">
    <AccountMultiSelect
      bind:value
      options={availableAccounts.map(a => ({ value: a, label: a }))}
      placeholder="All accounts" />
  </span>
{/if}

<style>
  /* Sits inside a flex header. `margin-left: auto` claims the
     spacer slot so the close-button / CardControls cluster sits
     to the right of the dropdown without needing its own auto.
     Consumers can override via :global if their header wants a
     different layout. */
  .act-acct {
    display: inline-flex;
    align-items: center;
    margin-left: auto;
    font-size: 0.7rem;
    min-width: 11rem;
  }
  :global(.act-acct .multiselect-trigger),
  :global(.act-acct .multiselect-control) { min-width: 11rem; }
</style>
