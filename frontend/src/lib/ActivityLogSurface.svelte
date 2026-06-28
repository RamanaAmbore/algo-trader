<!--
  ActivityLogSurface — the configured LogPanel used by every
  activity-card surface (ActivityLogModal, /orders Activity card,
  future /console surfaces). Encapsulates the canonical config
  so callers don't repeat the same 6-prop dance:

      hideInlineAccountFilter = true     (header dropdown lives in the parent)
      bind:accountFilter                 (state owned by parent)
      bind:availableAccounts             (state mirrored to parent)
      multiColumn = true                 (2-col layout on wide containers)

  Pair with `ActivityAccountSelect` for the header dropdown — same
  bindable state is threaded through both.

  Usage:
    <ActivityLogSurface
      bind:accountFilter={_accountFilter}
      bind:availableAccounts={_availableAccounts}
      defaultTab="order"
      statusFilter={_statusFilter}
      symbolFilter={symFilter} />
-->
<script>
  import LogPanel from '$lib/LogPanel.svelte';

  let {
    /** Tab to land on. Modal threads its `initialTab` here; pages
     *  pick whatever makes sense for their surface (orders → 'order'). */
    defaultTab          = 'order',
    /** Status filter chip from /orders counter cards. */
    statusFilter        = /** @type {'all'|'open'|'complete'|'rejected'|'cancelled'} */ ('all'),
    /** Set of upper-case tradingsymbols to scope Order tab rows to. */
    symbolFilter        = /** @type {Set<string> | null} */ (null),
    /** Poll cadence — ms between auto-refreshes inside LogPanel. */
    pollMs              = 3000,
    /** Tail height passthrough — LogPanel respects the same class. */
    heightClass         = 'flex-1 min-h-0',
    /** Selected account codes — bindable so parent + ActivityAccountSelect share state. */
    accountFilter       = $bindable(/** @type {string[]} */ ([])),
    /** Account codes from current order rows — bindable so parent renders the dropdown. */
    availableAccounts   = $bindable(/** @type {string[]} */ ([])),
    /** Active log level filter — bindable so parent's ActivityHeaderFilters
     *  drives it. Default 'error' per operator request — only loud
     *  rows by default; 'all' for the full paper trail. */
    levelFilter         = $bindable(/** @type {'all'|'error'|'warning'|'info'} */ ('all')),
  } = $props();
</script>

<LogPanel
  {heightClass}
  {defaultTab}
  {statusFilter}
  {symbolFilter}
  {pollMs}
  hideInlineAccountFilter={true}
  bind:accountFilter
  bind:availableAccounts
  {levelFilter}
  multiColumn={true} />
