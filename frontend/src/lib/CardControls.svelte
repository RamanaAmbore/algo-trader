<!--
  CardControls — canonical card-chrome control cluster.

  Composes the five card buttons every page-section card uses, in
  canonical order: Refresh (fullscreen only) · Search · Collapse ·
  DefaultSize · Fullscreen. Replaces the 5-line hand-rolled cluster
  that was duplicated across MarketPulse, /dashboard, /orders,
  /admin/derivatives, /admin/research, /automation/*.

  Operator: "have search, expand/contract, full screen card as a
  reusable code".

  Props:
    isCollapsed, isFullscreen — bindable card state (drives + flows
        from each button's $bindable).
    filter — bindable GridSearchButton filter text. Omit if the
        card has no searchable grid (component hides the button).
    cardId — localStorage key for CollapseButton persistence
        (omit to skip persistence; collapse still works in-session).
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
  import CollapseButton from '$lib/CollapseButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import GridDownloadButton from '$lib/GridDownloadButton.svelte';
  import GridSearchButton from '$lib/GridSearchButton.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';

  /** @type {{
   *   isCollapsed?: boolean,
   *   isFullscreen?: boolean,
   *   filter?: string,
   *   cardId?: string,
   *   label?: string,
   *   onRefresh?: ((...args: any[]) => void | Promise<void>) | null,
   *   refreshLoading?: boolean,
   *   showSearch?: boolean,
   *   refreshAlwaysVisible?: boolean,
   *   showCollapse?: boolean,
   *   onDownload?: ((...args: any[]) => void) | null,
   *   hideFullscreen?: boolean,
   * }} */
  let {
    isCollapsed = $bindable(false),
    isFullscreen = $bindable(false),
    filter = $bindable(''),
    cardId = '',
    label = 'card',
    onRefresh = null,
    refreshLoading = $bindable(false),
    showSearch = true,
    refreshAlwaysVisible = false,
    showCollapse = true,
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
{#if showCollapse}
  <CollapseButton bind:isCollapsed {cardId} {label} />
{/if}
{#if !hideFullscreen || isFullscreen}
  {#if !isFullscreen}
    <FullscreenButton bind:isFullscreen {label} />
  {:else}
    <DefaultSizeButton bind:isFullscreen bind:isCollapsed {label} />
  {/if}
{/if}
