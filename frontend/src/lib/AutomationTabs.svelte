<!--
  AutomationTabs — shared tab strip linking the surfaces that
  collectively form the Automation workspace. Dropped at the top of:
    /automation              → Agents rules list (event-driven)
    /automation/templates    → Order Templates (per-order config — Phase 3)
    /automation/activity     → Recent fires (agent_fire / action events)
    /admin/tokens            → Grammar tokens catalog (admin)
    /automation/fragments    → Reusable sub-trees ($ref-able)
    /admin/research          → Lab (Claude Code + MCP research, admin)

  Operator mental model: Agents (event-driven) and Templates (per-order
  config) sit side by side at the top — both decide HOW the engine acts
  on positions. Activity / Tokens / Fragments / Lab are the supporting
  surfaces (history, vocabulary, reuse, exploration).

  Industry analogue: TradingView "Alerts & Automations" tab strip;
  NinjaTrader Control Center workspace. Renamed from
  AgentWorkspaceTabs in v2.1 when Templates joined the workspace.
-->
<script>
  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  // Order = operator-workflow order: the two configuration surfaces
  // first (Agents, Templates), then history (Activity), then building
  // blocks (Tokens, Fragments), then exploration (Lab).
  const TABS = [
    { href: '/automation',           label: 'Agents'    },
    { href: '/automation/templates', label: 'Templates' },
    { href: '/automation/activity',  label: 'Activity'  },
    { href: '/admin/tokens',         label: 'Tokens'    },
    { href: '/automation/fragments', label: 'Fragments' },
    { href: '/admin/research',       label: 'Lab'       },
  ];

  // Longest-match — /automation/activity must beat /automation.
  const activeHref = $derived.by(() => {
    const path = page.url.pathname;
    let best = '';
    for (const t of TABS) {
      if ((path === t.href || path.startsWith(t.href + '/')) && t.href.length > best.length) {
        best = t.href;
      }
    }
    return best;
  });
</script>

<nav class="aw-tabs" aria-label="Automation workspace">
  {#each TABS as t}
    <button
      class="aw-tab {activeHref === t.href ? 'aw-tab-active' : ''}"
      onclick={() => goto(t.href)}
      type="button"
    >{t.label}</button>
  {/each}
</nav>

<style>
  .aw-tabs {
    display: flex;
    align-items: center;
    gap: 0.15rem;
    padding: 0.15rem 0.2rem;
    margin: 0 0 0.6rem 0;
    border-bottom: 1px solid rgba(251,191,36,0.18);
    background: linear-gradient(180deg, rgba(15,23,41,0.7) 0%, rgba(10,16,32,0.7) 100%);
    border-radius: 0.25rem 0.25rem 0 0;
  }
  .aw-tab {
    padding: 0.35rem 0.85rem;
    font-size: 0.72rem;
    font-weight: 500;
    color: rgba(180,200,230,0.75);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    cursor: pointer;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
    transition: color 0.06s, border-bottom-color 0.06s, background-color 0.06s;
    outline: none !important;
    margin-bottom: -1px; /* stitch the tab's bottom border flush with the strip's. */
  }
  .aw-tab:hover {
    color: #fbbf24;
    background: rgba(251,191,36,0.06);
  }
  .aw-tab-active {
    color: #fbbf24;
    font-weight: 700;
    border-bottom-color: #fbbf24;
  }
</style>
