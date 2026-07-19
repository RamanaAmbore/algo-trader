<!--
  /automation/activity — recent agent fires + action events.

  Surfaces the same UnifiedLog the dashboard renders, but lifted out
  of the P&L-focused dashboard into a dedicated tab inside the
  Automation workspace. Operator going "what fired today?" lands here
  directly instead of scrolling past P&L analysis on /dashboard.
-->
<script>
  import { nowStamp, lastRefreshAt, formatIstOnly } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import ActionEventsToggle from '$lib/ActionEventsToggle.svelte';
  import CardHeader from '$lib/CardHeader.svelte';

  // Match the dashboard's default — fires only, with an opt-in to
  // surface action successes / errors.
  let showActions  = $state(false);
  let _bump        = $state(0);
  let _showLiveTs  = $state(false);
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
  <span class="algo-ts-group" onclick={() => { if ($lastRefreshAt) _showLiveTs = !_showLiveTs; }} onkeydown={(e) => { if ($lastRefreshAt && (e.key === "Enter" || e.key === " ")) _showLiveTs = !_showLiveTs; }} role="button" tabindex="0">
    <span class="algo-ts"
          class:algo-ts-hidden={!!$lastRefreshAt && _showLiveTs}
          title={$lastRefreshAt ? 'Live clock — tap to switch' : 'Live clock'}>
      {$nowStamp}
    </span>
    {#if $lastRefreshAt}
      <span class="algo-ts-vsep" aria-hidden="true">|</span>
      <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}>
        {formatIstOnly($lastRefreshAt)}
      </span>
    {/if}
  </span>
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
  <CardHeader
    bind:isCollapsed={_colActivity}
    bind:isFullscreen={_fsActivity}
    cardId="automation-activity"
    label="Activity"
    onRefresh={_onRefresh}
    refreshLoading={_refreshing}
    showSearch={false}
  >
    {#snippet left()}
      <span class="mp-section-label">Agent Activity</span>
    {/snippet}
    {#snippet right()}
      <ActionEventsToggle bind:value={showActions} />
    {/snippet}
  </CardHeader>
  <div class="card-body" hidden={_colActivity}>
    <UnifiedLog
      filter={{ kinds }}
      excludeSim={true}
      maxRows={100}
      bump={_bump}
      emptyMessage="No agent fires yet today." />
  </div>
</section>

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
</style>

