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
  /* Sits inside a flex header. Right-alignment is handled by the
     parent flex container (ch-right or equivalent), not by
     margin-left: auto here.

     Mobile: NO min-width override — defer to AccountMultiSelect's
     internal media query (5.6rem→4.4rem at ≤600px) so the
     dropdown fits alongside title + level + close in the activity
     modal header without overlap. Operator: "on mobile, there is
     more space on title. keep the drop downs on header without
     overlapping or pushing x button on modal on mobile." */
  .act-acct {
    display: inline-flex;
    align-items: center;
    font-size: var(--fs-lg);
  }
  @media (min-width: 520px) {
    .act-acct { min-width: 11rem; }
    :global(.act-acct .multiselect-trigger),
    :global(.act-acct .multiselect-control) { min-width: 11rem; }
  }
</style>
