<!--
  /automation/activity — recent agent fires + action events.

  Uses ActivityLogSurface (same as every other activity surface in the
  algo app) with defaultTab="agent" so the operator lands on the Agent
  log tab directly.
-->
<script>
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';

  let _refreshing       = $state(false);
  let _accountFilter    = $state('');
  let _availableAccounts = $state([]);
  let _levelFilter      = $state('all');

  function _onRefresh() {
    _refreshing = true;
    setTimeout(() => { _refreshing = false; }, 400);
  }
</script>

<svelte:head>
  <title>Agent Activity | RamboQuant Analytics</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Agent Activity</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={_onRefresh} loading={_refreshing} label="activity" />
    <PageHeaderActions />
  </span>
</div>

<AutomationTabs />

<ActivityLogSurface
  context="page"
  label="ACTIVITY"
  defaultTab="agent"
  cardId="automation-activity"
  bind:accountFilter={_accountFilter}
  bind:availableAccounts={_availableAccounts}
  bind:levelFilter={_levelFilter}
  onRefresh={_onRefresh}
  bind:refreshLoading={_refreshing} />

<style>
</style>

