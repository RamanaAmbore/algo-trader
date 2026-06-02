<!--
  AccountMultiSelect — broker-account picker shared across every
  algo card that filters per-account positions/holdings.

  Global policy (read this before adding the picker anywhere):
    1. Show the picker whenever the host card displays positions OR
       holdings (regardless of whether the operator has any open
       positions today — the affordance stays visible).
    2. DISABLE the picker when the active context is NOT
       positions/holdings. Examples:
         - Top Winners/Losers cards on Underlying / Midcap / Smallcap
           tabs (market-wide universes).
         - Pulse card when both Positions + Holdings source toggles
           are off (the unified grid then shows only watchlist + pinned
           + movers — no per-account scoping makes sense).
    3. Disabled-state carries a tooltip explaining why ("Applies only
       to Positions or Holdings tabs / sources").

  Bind the value array as you would any MultiSelect:
    <AccountMultiSelect
      bind:value={selectedAccounts}
      options={availableAccounts.map(a => ({ value: a, label: a }))}
      disabled={!showingPositionsOrHoldings} />

  Component just composes MultiSelect with the agreed defaults +
  tooltip text. Keep this thin so audits of the policy stay easy.
-->
<script>
  import MultiSelect from '$lib/MultiSelect.svelte';

  let {
    /** bindable selection array. Empty = all accounts (no filter). */
    value = $bindable(/** @type {string[]} */ ([])),
    /** [{ value, label }] — accounts available in current dataset. */
    options = [],
    /** Disable the picker when the current view is not per-account. */
    disabled = false,
    /** Tooltip shown when disabled. */
    disabledReason = 'Account filter applies only to Positions or Holdings views',
    /** Tooltip shown when enabled. */
    enabledHint = 'Filter by broker account',
    /** Empty-state label. */
    placeholder = 'All accounts',
    /** Accessibility label. */
    ariaLabel = 'Filter by broker account',
    /** Theme passthrough (dark/light). */
    theme = 'dark',
    /** Optional DOM id (for label `for=` linking). */
    id = '',
  } = $props();
</script>

<div class="ams" title={disabled ? disabledReason : enabledHint}>
  <MultiSelect
    bind:value
    {options}
    {disabled}
    {placeholder}
    {ariaLabel}
    {theme}
    {id} />
</div>

<style>
  /* No own chrome — MultiSelect carries the styling. Wrapper exists
     only to host the title= tooltip so disabled-state hover still
     shows the explanation (disabled <button> elements don't fire
     hover events reliably across browsers). */
  /* Account codes are max 6 chars (ZG####) — earlier 7.5rem/14rem
     min/max reserved space the trigger never used. Tightened so the
     picker bar can fit more siblings on one row before wrapping. */
  .ams {
    min-width: 5rem;
    max-width: 9rem;
    flex-shrink: 1;
  }
  @media (max-width: 600px) {
    .ams {
      min-width: 4rem;
      max-width: 6.5rem;
    }
  }
</style>
