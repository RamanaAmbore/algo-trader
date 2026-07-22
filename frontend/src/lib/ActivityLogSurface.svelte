<!--
  ActivityLogSurface — the SSOT LogPanel wrapper used by every
  activity surface in the algo app (ActivityLogModal, /orders activity
  card, /automation, /console, SymbolPanel bottom panel,
  ReplayPanel, SimulatorPanel, and the /activity page).

  Encapsulates the canonical config so callers don't repeat the same
  prop dance:

      hideInlineAccountFilter = true     (header dropdown lives in the parent,
                                          override to false for inline-filter mounts)
      bind:accountFilter                 (state owned by parent)
      bind:availableAccounts             (state mirrored to parent)
      multiColumn = (context-derived)    (2-col only on wide containers;
                                          override with `multiColumn` prop)

  Pair with `ActivityAccountSelect` for the header dropdown — same
  bindable state is threaded through both.

  Usage (header-driven):
    <ActivityLogSurface
      bind:accountFilter={_accountFilter}
      bind:availableAccounts={_availableAccounts}
      defaultTab="order"
      context="page"
      statusFilter={_statusFilter}
      symbolFilter={symFilter} />

  Usage (inline-filter, no parent header):
    <ActivityLogSurface
      context="card"
      hideInlineAccountFilter={false}
      defaultTab="terminal"
      onTabChange={(id) => { logTab = id; }} />
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
     *  drives it. Default 'all' keeps pre-filter behaviour; surfaces that
     *  want loud-rows-only pass 'error'. */
    levelFilter         = $bindable(/** @type {'all'|'error'|'warning'|'info'} */ ('all')),
    /** Surface context — gates the context-derived 2-column magazine flow.
     *  Overridden entirely when `multiColumn` is provided explicitly.
     *  @type {'page'|'card'|'card-wide'|'modal'} */
    context             = /** @type {'page'|'card'|'card-wide'|'modal'} */ ('page'),
    /** Optional label for the LogPanel header chip (e.g. "ACTIVITY"). */
    label               = /** @type {string} */ (''),
    /** Bindable collapse state — passed through to LogPanel. */
    isCollapsed         = $bindable(false),
    /** Bindable tall/expanded state — passed through to LogPanel. */
    isTall              = $bindable(false),
    /** Retained bindable for caller compat. Not passed to LogPanel —
     *  LogPanel now uses in-place height expansion (isTall) instead. */
    isFullscreen        = $bindable(false),
    /** Refresh callback — passed through to LogPanel. */
    onRefresh           = /** @type {(() => void) | null} */ (null),
    /** Bindable refresh-loading spinner state. */
    refreshLoading      = $bindable(false),
    /** Download callback — passed through to LogPanel. */
    onDownload          = /** @type {(() => void) | null} */ (null),
    /** Card id for CollapseButton localStorage persistence. */
    cardId              = /** @type {string} */ (''),
    /** Close callback — used in modal context. */
    onClose             = /** @type {(() => void) | null} */ (null),
    /**
     * Explicit multiColumn override. When provided (true or false), this
     * value is used directly and ignores the context-derived default.
     * When omitted (undefined), the context determines the value:
     *   - 'page' | 'modal' | 'card-wide' → true
     *   - 'card' → false
     * @type {boolean | undefined}
     */
    multiColumn         = /** @type {boolean | undefined} */ (undefined),
    /**
     * Scope the sim/agent log view to the running simulation only.
     * Passed through to LogPanel unchanged.
     */
    simScope            = false,
    /**
     * Callback fired when the operator switches the active log tab.
     * @type {(tab: string) => void}
     */
    onTabChange         = /** @type {(tab: string) => void} */ (() => {}),
    /**
     * Execution mode string (sim / paper / live / shadow / replay).
     * When set, LogPanel auto-flips the tab and applies the matching
     * order filter. Passed through unchanged.
     * @type {string | null}
     */
    mode                = /** @type {string | null} */ (null),
    /**
     * Whether to gate all activity tabs by the current executionMode
     * from the global store. Default true; set false on surfaces that
     * want a cross-mode view.
     */
    gateByMode          = true,
    /**
     * Command history entries fed into the Terminal tab
     * (CommandLineTab integration).
     * @type {Array<{status: string, message: string, fields?: Record<string,string>, time: string}>}
     */
    cmdHistory          = /** @type {Array<{status: string, message: string, fields?: Record<string,string>, time: string}>} */ ([]),
    /**
     * Subset of tabs to display. Defaults to the canonical full set
     * inherited from LogPanel. Only override when a page genuinely
     * needs a reduced tab strip.
     * @type {string[] | undefined}
     */
    tabs                = /** @type {string[] | undefined} */ (undefined),
    /**
     * Hide the inline account dropdown in the tab row.
     * Default true — callers that provide their own header dropdown
     * (ActivityLogModal, /activity page) pass no value or true.
     * Simple mounts that want the inline filter (SymbolPanel bottom
     * panel, execution panels) pass false.
     * Retained for backwards compat; no longer used for rendering
     * (ActivityHeaderFilters is always rendered in LogPanel).
     */
    hideInlineAccountFilter = true,
  } = $props();

  // Two-column magazine flow on wider containers. Enabled for:
  //   - 'page'      — the full-width /activity route
  //   - 'modal'     — ActivityLogModal spans up to 96vw
  //   - 'card-wide' — dashboard's activity card (row-wide on desktop)
  // Narrow 'card' contexts (orders inline sidebar card) stay single
  // column since their visible container is under the 900px threshold
  // regardless of viewport width. LogPanel's @media (max-width: 900px)
  // handles responsive collapse to single column when viewport shrinks.
  //
  // Explicit `multiColumn` prop overrides context entirely.
  const _multiColumn = $derived(
    multiColumn !== undefined
      ? multiColumn
      : (context === 'page' || context === 'modal' || context === 'card-wide')
  );
</script>

<LogPanel
  {heightClass}
  {defaultTab}
  {statusFilter}
  {symbolFilter}
  {pollMs}
  {hideInlineAccountFilter}
  bind:accountFilter
  bind:availableAccounts
  bind:levelFilter
  multiColumn={_multiColumn}
  {simScope}
  {onTabChange}
  {mode}
  {gateByMode}
  {cmdHistory}
  {tabs}
  {context}
  {label}
  bind:isCollapsed
  bind:isTall
  {onRefresh}
  bind:refreshLoading
  {onDownload}
  {cardId}
  {onClose}
/>
