<script>
  /**
   * /activity — standalone Activity surface.
   *
   * Same composition as ActivityLogModal + the /orders Activity card:
   *
   *    ActivityHeaderFilters  (account + level dropdowns, header)
   *    ActivityLogSurface     (configured LogPanel, body)
   *
   * Operator: "Do you think activity should also have a separate
   * [page]. presently it is a modal and card." This is that page —
   * bookmarkable, full-viewport, no modal chrome. Same shared
   * components, so the three surfaces (modal, card, page) can't
   * drift on filter UI or LogPanel config.
   */
  import { page } from '$app/state';
  import { authStore, nowStamp } from '$lib/stores';
  import { selectedStrategyId, strategyOpenSymbols } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import ActivityHeaderFilters from '$lib/ActivityHeaderFilters.svelte';
  import BellIcon from '$lib/icons/BellIcon.svelte';

  const isDemo = $derived(!$authStore.user);

  /** @type {string[]} */
  let _accountFilter     = $state([]);
  /** @type {string[]} */
  let _availableAccounts = $state([]);
  /** @type {'all'|'error'|'warning'|'info'} */
  let _levelFilter       = $state('all');

  // Read ?tab=... so the navbar broker chip and other deep-links can
  // open this page on a specific tab. Whitelist against the known tab
  // ids so a typo / hostile URL can't end up showing a broken tab.
  const _ALLOWED_TABS = ['order','agent','terminal','simulator','system','conn','news'];
  const _urlTab = $derived.by(() => {
    const t = page?.url?.searchParams?.get('tab') || '';
    return _ALLOWED_TABS.includes(t) ? t : 'order';
  });

  // Manual-refresh bump — clicking the page-header Refresh icon
  // rotates the badge and asks ActivityLogSurface to re-poll
  // immediately (LogPanel re-mounts on key change, which is the
  // cheapest way to force a fresh fetch on every tab).
  let _refreshKey = $state(0);
  let _refreshing = $state(false);
  function _refresh() {
    _refreshing = true;
    _refreshKey++;
    setTimeout(() => { _refreshing = false; }, 400);
  }
</script>

<svelte:head>
  <title>Activity · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <BellIcon width="14" height="14" class="page-title-icon" />
    Activity
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <!-- Filter chips are LEFT-aligned per canonical header rule
       ([Title] [Tabs?] [AccountMultiSelect?] [Chips?] → spacer →
       [Trio]). Only Refresh + Order + Chart + Activity + Collapse +
       Fullscreen + Default-size icons sit RIGHT of ml-auto.
       ActivityHeaderFilters' built-in margin-left:auto is overridden
       in the <style> block below for this page-header context. -->
  <ActivityHeaderFilters
    bind:accountFilter={_accountFilter}
    bind:levelFilter={_levelFilter}
    availableAccounts={_availableAccounts} />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={_refresh} loading={_refreshing} label="activity" />
    <PageHeaderActions />
  </span>
</div>

<section class="activity-page-body">
  {#key _refreshKey}
    <ActivityLogSurface
      heightClass="activity-page-rows"
      defaultTab={_urlTab}
      context="page"
      symbolFilter={$selectedStrategyId == null ? null : $strategyOpenSymbols}
      bind:accountFilter={_accountFilter}
      bind:availableAccounts={_availableAccounts}
      bind:levelFilter={_levelFilter} />
  {/key}
</section>

<style>
  :global(.page-title-icon) { color: var(--c-action); flex-shrink: 0; }
  /* Neutralize ActivityHeaderFilters' built-in `margin-left: auto`
     when mounted inside this page-header. The component's auto-margin
     is designed for card-headers (where it pushes CardControls to the
     right). In the page-header context, the canonical .ml-auto spacer
     handles the right-push, so the filters themselves should sit
     flush-LEFT against the timestamp per the canonical
     [Title][Chips?] → spacer → [Trio] rule. */
  :global(.page-header .act-filters) {
    margin-left: 0;
  }
  .activity-page-body {
    display: flex;
    flex-direction: column;
    flex: 1 1 0;
    min-height: 0;
    margin: 0 0.5rem 0.5rem 0.5rem;
    background: rgba(0, 0, 0, 0.18);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 4px;
    padding: 0.5rem;
  }
  :global(.activity-page-rows) {
    /* Fill the page body — operator gets a full-viewport reading
       surface here, unlike the modal/card which are bounded. */
    flex: 1 1 0 !important;
    min-height: 0;
    height: 100% !important;
  }
</style>
