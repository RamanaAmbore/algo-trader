<!--
  /agents/activity — recent agent fires + action events.

  Surfaces the same UnifiedLog the dashboard renders, but lifted out
  of the P&L-focused dashboard into a dedicated tab inside the agent
  workspace. Operator going "what fired today?" lands here directly
  instead of scrolling past P&L analysis on /dashboard.
-->
<script>
  import { nowStamp } from '$lib/stores';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import AgentWorkspaceTabs from '$lib/AgentWorkspaceTabs.svelte';

  // Match the dashboard's default — fires only, with an opt-in to
  // surface action successes / errors.
  let showActions = $state(false);
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
  <h1 class="page-title-chip">Agent Activity</h1>
  <InfoHint popup text="Recent agent fires (and optionally action successes/errors). Real-pipeline events only — sim runs filtered out. Click a row's slug to jump to that agent on the Agents tab." />
  <span class="algo-ts ml-auto">{$nowStamp}</span>
</div>

<AgentWorkspaceTabs />

<section class="algo-status-card p-3">
  <div class="filter-row">
    <button
      class="filter-btn"
      class:filter-btn-on={showActions}
      onclick={() => showActions = !showActions}
      type="button">
      {showActions ? '✓' : ''} include action events
    </button>
    <span class="filter-hint">
      {showActions
        ? 'showing fires + action successes/errors'
        : 'showing fires only — toggle to include actions'}
    </span>
  </div>
  <UnifiedLog
    filter={{ kinds }}
    excludeSim={true}
    maxRows={100}
    emptyMessage="No agent fires yet today." />
</section>

<style>
  .filter-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.25rem 0 0.45rem;
    flex-wrap: wrap;
  }
  .filter-btn {
    padding: 0.2rem 0.55rem;
    font-size: 0.65rem;
    font-weight: 500;
    color: rgba(180,200,230,0.75);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 0.25rem;
    cursor: pointer;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.03em;
    transition: background-color 0.06s, color 0.06s, border-color 0.06s;
  }
  .filter-btn:hover {
    background: rgba(251,191,36,0.08);
    color: #fbbf24;
    border-color: rgba(251,191,36,0.3);
  }
  .filter-btn-on {
    background: rgba(251,191,36,0.14);
    color: #fbbf24;
    border-color: rgba(251,191,36,0.5);
  }
  .filter-hint {
    font-size: 0.6rem;
    color: rgba(180,200,230,0.55);
    font-family: ui-monospace, monospace;
    letter-spacing: 0.02em;
  }
</style>
