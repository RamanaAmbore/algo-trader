<!--
  /automation/activity — recent agent fires + action events.

  Surfaces the same UnifiedLog the dashboard renders, but lifted out
  of the P&L-focused dashboard into a dedicated tab inside the
  Automation workspace. Operator going "what fired today?" lands here
  directly instead of scrolling past P&L analysis on /dashboard.
-->
<script>
  import { nowStamp } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import ActionEventsToggle from '$lib/ActionEventsToggle.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import DefaultSizeButton from '$lib/DefaultSizeButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';

  // Match the dashboard's default — fires only, with an opt-in to
  // surface action successes / errors.
  let showActions  = $state(false);
  let _bump        = $state(0);
  let _refreshing  = $state(false);
  let _colActivity = $state(false);
  let _fsActivity  = $state(false);

  function _onRefresh() {
    _refreshing = true;
    _bump++;
    setTimeout(() => { _refreshing = false; }, 400);
  }
  const kinds = $derived(
    showActions
      ? ['agent_fire', 'agent_action_success', 'agent_action_error']
      : ['agent_fire']
  );
</script>

<svelte:head>
  <title>Agent Activity | RamboQuant Analytics</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Agent Activity</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={_onRefresh} loading={_refreshing} label="activity" />
    <PageHeaderActions />
  </span>
</div>

<AutomationTabs />

<section class="bucket-card"
  class:fs-card-on={_fsActivity}
  class:is-collapsed={_colActivity}>
  <div class="bucket-header">
    <span class="mp-section-label">Agent Activity</span>
    <ActionEventsToggle bind:value={showActions} />
    {#if _fsActivity}
      <RefreshButton onClick={_onRefresh} loading={_refreshing} label="activity" />
    {/if}
    <CollapseButton bind:isCollapsed={_colActivity} cardId="automation-activity" label="Activity" />
    <DefaultSizeButton bind:isFullscreen={_fsActivity} bind:isCollapsed={_colActivity} label="Activity" />
    <FullscreenButton bind:isFullscreen={_fsActivity} label="Activity" />
  </div>
  <div class="card-body" hidden={_colActivity}>
    <UnifiedLog
      filter={{ kinds }}
      excludeSim={true}
      maxRows={100}
      bump={_bump}
      emptyMessage="No agent fires yet today." />
  </div>
</section>

