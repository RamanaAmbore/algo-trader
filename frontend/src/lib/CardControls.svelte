<!--
  CardControls — canonical card-chrome control cluster.

  Composes the card buttons every page-section card uses, in
  canonical order: Refresh (fullscreen only) · Search · DefaultSize ·
  Fullscreen. Replaces the 5-line hand-rolled cluster that was
  duplicated across cards.

  Collapse removed 2026-10-13 (header button-group standardization) —
  the chevron caret that inverted on toggle was flagged as visual
  clutter. `isCollapsed`/`showCollapse` (this file) and `CardHeader.svelte`'s
  own matching props were fully removed in the same sweep, confirmed
  via a fresh app-wide grep that zero callers remained.

  Adoption (verified 2026-10, do not restate without re-checking):
    Direct import — MarketPulse, /dashboard, /admin/derivatives,
      PerformancePage (Positions/Holdings Summary + Breakdown cards).
    Indirect, via LogPanel.svelte's own CardControls import — /orders
      (renders LogPanel directly) and /automation (renders ActivityLogSurface,
      which wraps LogPanel).
    Automation's Console tab (ConsoleTab.svelte, 2026-10) — wraps the CLI
      entry + result area in a CardHeader with ONLY Fullscreen wired
      (showSearch false — no grid to search). Operator request: give
      the CLI panel the same card chrome every other operator-action
      card has.
    NOT adopted on pages without actionable grids — /admin/mcp, etc.

  Operator: "have search, expand/contract, full screen card as a
  reusable code".

  Props:
    isFullscreen — bindable card state (drives + flows from each
        button's $bindable).
    filter — bindable GridSearchButton filter text. Omit if the
        card has no searchable grid (component hides the button).
    cardId — currently unused by any button here (Collapse was its
        only consumer); kept as a prop since CardHeader still forwards
        it through for potential future per-card persistence needs.
    label — card name used in every button's a11y title + aria-label.
    onRefresh — async function fired by RefreshButton. Required to
        render RefreshButton; otherwise refresh slot is skipped.
    refreshLoading — bindable spinner state passed to RefreshButton.
    showSearch — boolean (default true). Set false on cards that
        have no searchable grid (e.g. Chart cards, News strip).
    refreshAlwaysVisible — boolean (default false). By default the
        RefreshButton renders ONLY when isFullscreen is true (canonical
        chrome rule: refresh is fullscreen-only since the page-header
        already exposes a workspace-wide refresh). Set true to keep
        it visible in non-fullscreen mode for cards that own their
        refresh path independently (e.g. /orders activity log).
-->
<script>
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import GridDownloadButton from '$lib/GridDownloadButton.svelte';
  import GridSearchButton from '$lib/GridSearchButton.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';

  /** @type {{
   *   isFullscreen?: boolean,
   *   filter?: string,
   *   cardId?: string,
   *   label?: string,
   *   onRefresh?: ((...args: any[]) => void | Promise<void>) | null,
   *   refreshLoading?: boolean,
   *   showSearch?: boolean,
   *   refreshAlwaysVisible?: boolean,
   *   onDownload?: ((...args: any[]) => void) | null,
   *   hideFullscreen?: boolean,
   * }} */
  let {
    isFullscreen = $bindable(false),
    filter = $bindable(''),
    cardId = '',
    label = 'card',
    onRefresh = null,
    refreshLoading = $bindable(false),
    showSearch = true,
    refreshAlwaysVisible = false,
    onDownload = null,
    hideFullscreen = false,
  } = $props();
</script>

{#if onRefresh && (isFullscreen || refreshAlwaysVisible)}
  <RefreshButton onClick={onRefresh} loading={refreshLoading} {label} />
{/if}
{#if showSearch}
  <GridSearchButton bind:filter {label} />
{/if}
<GridDownloadButton onClick={onDownload} {label} autoMargin={false} />
{#if !hideFullscreen || isFullscreen}
  {#if !isFullscreen}
    <FullscreenButton bind:isFullscreen {label} />
  {:else}
    <DefaultSizeButton bind:isFullscreen {label} />
  {/if}
{/if}
