<!--
  AutomationTabs — shared tab strip linking the surfaces that
  collectively form the Automation workspace. Dropped at the top of:
    /automation                 → Agents rules list (event-driven)
    /automation/templates       → Order Templates (TP/SL/Wing presets)
    /automation/agent-templates → Notify + Condition Templates ($ref-able)
    /automation/activity        → Recent fires (agent_fire / action events)
    /admin/tokens               → Grammar tokens catalog (admin)
    /admin/research             → Lab (Claude Code + MCP research, admin)

  Operator mental model: every reusable saved thing is a "template".
  Order Templates govern order attachments (TP/SL/Wing); Agent
  Templates govern agent composition (notify channels, condition
  sub-trees, action presets). Both come from the same family but
  serve different lifecycles — order templates pick at submit time,
  agent templates pick at agent-design time.

  Visual: renders through the canonical AlgoTabs primitive so the
  font-size, padding, letter-spacing, active-tab background tint,
  and hover treatment exactly match every other tab strip in the
  algo workspace (LogPanel, SymbolPanel, Dashboard, Derivatives,
  History, Execution, Research, MarketPulse). Operator: "tab
  decoration is not consistent across all pages" — prior hand-rolled
  `.aw-tab` ran 0.72rem with a gradient strip background unique to
  this nav; the AlgoTabs swap collapses it into the platform default.
-->
<script>
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import AlgoTabs from '$lib/AlgoTabs.svelte';

  const TABS = [
    { id: '/automation',                 label: 'Agents'          },
    { id: '/automation/templates',       label: 'Order Templates' },
    { id: '/automation/agent-templates', label: 'Agent Templates' },
    { id: '/automation/activity',        label: 'Activity'        },
    { id: '/admin/tokens',               label: 'Tokens'          },
    { id: '/admin/research',             label: 'Lab'             },
  ];

  // Longest-match — /automation/activity must beat /automation.
  const activeHref = $derived.by(() => {
    const path = page.url.pathname;
    let best = '';
    for (const t of TABS) {
      if ((path === t.id || path.startsWith(t.id + '/')) && t.id.length > best.length) {
        best = t.id;
      }
    }
    return best;
  });
</script>

<div class="aw-tabs-wrap">
  <AlgoTabs
    tabs={TABS}
    value={activeHref}
    onChange={(href) => goto(href)}
  />
</div>

<style>
  /* Thin wrapper carries only the bottom margin so consumers get a
     consistent gap before the page content (matches the previous
     `.aw-tabs { margin: 0 0 0.6rem 0 }`). Border-bottom + background
     gradient + padding are dropped — AlgoTabs supplies its own
     underline + transparent ground so the strip looks identical to
     every other in-page tab strip. */
  .aw-tabs-wrap {
    margin: 0 0 0.6rem 0;
  }
</style>
