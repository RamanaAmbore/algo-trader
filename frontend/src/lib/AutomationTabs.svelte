<!--
  AutomationTabs — shared tab strip for the Automation workspace.

  Sprint 9 (2026-10): /automation/templates and /automation/agent-templates
  were deleted outright and folded into THIS page as in-page tabs — Agents
  (agent rules), Brackets (order templates), Tokens (notify/condition
  fragments, renamed from "Agent Templates" — display-text only, the
  underlying model is unchanged). This component no longer navigates
  between routes; it's a thin AlgoTabs wrapper driven by the parent's own
  tab state via `value`/`onChange`, same contract as AlgoTabs itself.

  Sprint B (2026-10, Phase 2 "Console retirement"): added the Console
  tab — `/console` is retired as a standalone route; its full-scope
  agent-CLI + plain orders bar now lives here (ConsoleTab.svelte).

  Agent fire history lives on /activity (Agent tab), not in this strip.
-->
<script>
  import AlgoTabs from '$lib/AlgoTabs.svelte';

  /** @type {{ value: string, onChange: (id: string) => void }} */
  let { value, onChange } = $props();

  const TABS = [
    { id: 'console',  label: 'Console'  },
    { id: 'agents',   label: 'Agents'   },
    { id: 'brackets', label: 'Brackets' },
    { id: 'tokens',   label: 'Tokens'   },
  ];
</script>

<div class="aw-tabs-wrap">
  <AlgoTabs tabs={TABS} {value} {onChange} />
</div>

<style>
  /* Thin wrapper carries only the bottom margin so consumers get a
     consistent gap before the page content. Border-bottom + background
     gradient + padding are dropped — AlgoTabs supplies its own
     underline + transparent ground so the strip looks identical to
     every other in-page tab strip. */
  .aw-tabs-wrap {
    margin: 0 0 0.6rem 0;
  }
</style>
