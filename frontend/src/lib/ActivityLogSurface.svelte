<!--
  ActivityLogSurface — the configured LogPanel used by every
  activity-card surface (ActivityLogModal, /orders Activity card,
  future /console surfaces). Encapsulates the canonical config
  so callers don't repeat the same 6-prop dance:

      hideInlineAccountFilter = true     (header dropdown lives in the parent)
      bind:accountFilter                 (state owned by parent)
      bind:availableAccounts             (state mirrored to parent)
      multiColumn = (context === 'page') (2-col only on the full-width
                                          /activity page; modal + card
                                          surfaces stay single-column
                                          because their container is
                                          narrower than the 900px @media
                                          threshold even at 1280px+ viewport)

  Pair with `ActivityAccountSelect` for the header dropdown — same
  bindable state is threaded through both.

  Usage:
    <ActivityLogSurface
      bind:accountFilter={_accountFilter}
      bind:availableAccounts={_availableAccounts}
      defaultTab="order"
      context="page"
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
    /** Surface context — gates the 2-column magazine flow.
     *  @type {'page'|'card'|'card-wide'|'modal'} */
    context             = 'page',
  } = $props();

  // Two-column magazine flow on wider containers. Enabled for:
  //   - 'page'      — the full-width /activity route
  //   - 'modal'     — ActivityLogModal spans up to 96vw
  //   - 'card-wide' — dashboard's activity card (row-wide on desktop)
  // Narrow 'card' contexts (orders inline sidebar card) stay single
  // column since their visible container is under the 900px threshold
  // regardless of viewport width. LogPanel's @media (max-width: 900px)
  // handles responsive collapse to single column when viewport shrinks.
  const _multiColumn = $derived(
    context === 'page' || context === 'modal' || context === 'card-wide'
  );
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
  multiColumn={_multiColumn} />
