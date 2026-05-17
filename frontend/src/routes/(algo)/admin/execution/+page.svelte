<script>
  // Execution workspace (/admin/execution).
  //
  // Option B model (post-2026-05-17): this page is a workspace,
  // not a mode picker. Two tabs at the top — [Simulator] [Replay] —
  // each rendering its respective panel. The current master mode
  // (LIVE / PAPER / SHADOW) shows as a read-only chip in the header
  // for context; operators change it via the navbar dropdown.
  //
  // Why this split:
  //   - SIM and REPLAY are transient workspaces (the driver starts
  //     and stops; no persistent flag changes).
  //   - LIVE / PAPER / SHADOW are persistent master toggles that
  //     affect every broker-hitting action across the platform.
  //   - Mixing the two in one dropdown was confusing (the "what
  //     does REPLAY in the navbar mean?" complaint).
  //
  // Industry convention (IB TWS, ThinkOrSwim, QuantConnect, Lean,
  // NinjaTrader) — same split. Mode picker for persistent state;
  // dedicated workspaces for sim/backtest.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { authStore, clientTimestamp, executionMode } from '$lib/stores';
  import InfoHint       from '$lib/InfoHint.svelte';
  import SimulatorPanel from '$lib/execution/SimulatorPanel.svelte';
  import ReplayPanel    from '$lib/execution/ReplayPanel.svelte';

  const MODE_META = {
    sim:    { label: 'SIM',    color: '#fb7185', bg: 'rgba(251,113,133,0.12)', border: 'rgba(251,113,133,0.35)' },
    replay: { label: 'REPLAY', color: '#4ade80', bg: 'rgba(74,222,128,0.12)',  border: 'rgba(74,222,128,0.35)'  },
    paper:  { label: 'PAPER',  color: '#7dd3fc', bg: 'rgba(125,211,252,0.12)', border: 'rgba(125,211,252,0.35)' },
    shadow: { label: 'SHADOW', color: '#fb923c', bg: 'rgba(251,146,60,0.12)',  border: 'rgba(251,146,60,0.35)'  },
    live:   { label: 'LIVE',   color: '#6ee7b7', bg: 'rgba(110,231,183,0.12)', border: 'rgba(110,231,183,0.35)' },
  };

  const mode = $derived($executionMode);
  const meta = $derived(MODE_META[mode] || MODE_META.live);

  // Active tab — sim or replay. Seeded from ?tab= or ?mode=
  // (backward-compat for old SIM/REPLAY dropdown navigations).
  let tab = $state(/** @type {'sim'|'replay'} */ ('sim'));

  onMount(() => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    const params = page.url.searchParams;
    const want = params.get('tab') || params.get('mode');
    if (want === 'sim' || want === 'replay') tab = want;
  });

  function pickTab(/** @type {'sim'|'replay'} */ t) {
    tab = t;
    // Update URL so deep-links / refresh stay on the same tab.
    const url = new URL(page.url);
    url.searchParams.set('tab', t);
    url.searchParams.delete('mode');
    goto(url.pathname + url.search, { replaceState: true, noScroll: true });
  }
</script>

<svelte:head><title>Execution | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <div class="exec-header-left">
    <h1 class="algo-page-title">Execution</h1>
    <span class="exec-mode-pill"
          title="Master mode — change from the navbar dropdown"
          style="color:{meta.color}; background:{meta.bg}; border-color:{meta.border}">
      {meta.label}
    </span>
    <InfoHint popup text="Workspace for the two transient modes: <b>Simulator</b> (fabricated, scenario-driven) and <b>Replay</b> (historical-data backtest). The chip beside the title shows your current persistent master mode (PAPER / LIVE / SHADOW) — change it from the navbar." />
  </div>
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

<div class="exec-tabs" role="tablist" aria-label="Execution workspace">
  <button class="exec-tab" class:exec-tab-active={tab === 'sim'}
          role="tab" aria-selected={tab === 'sim'} onclick={() => pickTab('sim')}>
    Simulator
  </button>
  <button class="exec-tab" class:exec-tab-active={tab === 'replay'}
          role="tab" aria-selected={tab === 'replay'} onclick={() => pickTab('replay')}>
    Replay
    <span class="exec-tab-subtitle">historical backtest</span>
  </button>
</div>

{#if tab === 'sim'}
  <SimulatorPanel />
{:else if tab === 'replay'}
  <ReplayPanel />
{/if}

<style>
  .algo-page-title {
    font-size: 0.75rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-family: ui-monospace, monospace;
  }
  :global(.page-header:has(.algo-page-title)) {
    border-bottom: none;
    padding-bottom: 0;
    margin-bottom: 0.5rem;
  }
  .exec-header-left {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .exec-mode-pill {
    display: inline-flex;
    align-items: center;
    height: 1.3rem;
    padding: 0 0.55rem;
    border: 1px solid;
    border-radius: 9999px;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 800;
    letter-spacing: 0.1em;
    cursor: help;
  }
  .algo-ts {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    margin-left: auto;
  }
  /* Tab strip — [Simulator] [Replay]. Active tab carries an amber
     bottom border so the visual reads as a section header for the
     workspace below, not a dropdown trigger. */
  .exec-tabs {
    display: flex;
    gap: 0;
    margin-bottom: 0.75rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.18);
  }
  .exec-tab {
    appearance: none;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 0.5rem 1rem;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    color: #7e97b8;
    cursor: pointer;
    text-transform: uppercase;
    transition: color 0.1s, border-color 0.1s;
    display: inline-flex;
    align-items: baseline;
    gap: 0.45rem;
    margin-bottom: -1px;
  }
  .exec-tab:hover { color: #c8d8f0; }
  .exec-tab-active {
    color: #fbbf24;
    border-bottom-color: #fbbf24;
  }
  .exec-tab-subtitle {
    font-size: 0.55rem;
    font-weight: 500;
    color: #7e97b8;
    text-transform: none;
    letter-spacing: 0.02em;
  }
  .exec-tab-active .exec-tab-subtitle { color: #fde68a; }
</style>
