<script>
  // Execution workspace (/admin/execution).
  //
  // Scope (P2 = B): SIM and REPLAY only. Both modes need a control
  // surface (scenario picker, Start/Stop, log) that lives nowhere
  // else. PAPER / LIVE / SHADOW are master-toggle modes — they have
  // no dedicated workspace; their content lives on /orders (filtered
  // by mode), /dashboard (P&L), and /agents (fires). Picking PAPER /
  // LIVE / SHADOW from the navbar flips the master toggle without
  // navigating; if the operator deep-links to /admin/execution while
  // in one of those modes, we render a brief explainer.
  //
  // Mode source of truth: the global `executionMode` store driven by
  // the navbar dropdown. The internal mode dropdown that used to live
  // here was removed (P1).

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { authStore, clientTimestamp, executionMode } from '$lib/stores';
  import { setExecutionMode } from '$lib/api';
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
  const meta = $derived(MODE_META[mode] || MODE_META.sim);

  onMount(async () => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    // Seed the store from ?mode= so deep-links work.
    const param = page.url.searchParams.get('mode');
    const valid = ['sim', 'replay', 'paper', 'shadow', 'live'];
    if (param && valid.includes(param)) {
      try { await setExecutionMode(param); } catch (_) { executionMode.set(/** @type {any} */ (param)); }
    }
  });
</script>

<svelte:head><title>Execution | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <div class="exec-header-left">
    <h1 class="algo-page-title">Execution</h1>
    <span class="exec-mode-pill"
          style="color:{meta.color}; background:{meta.bg}; border-color:{meta.border}">
      {meta.label}
    </span>
    <InfoHint popup text="The execution workspace hosts the two modes that need a control surface: <b>SIM</b> (scenario picker, iteration form, charts) and <b>REPLAY</b> (historical candle backtest). PAPER · LIVE · SHADOW are master-toggle modes — switch from the navbar; their order log lives at /orders, P&L at /dashboard." />
  </div>
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

{#if mode === 'sim'}
  <SimulatorPanel />
{:else if mode === 'replay'}
  <ReplayPanel />
{:else}
  <!-- PAPER / LIVE / SHADOW have no dedicated workspace. -->
  <div class="exec-info">
    <h2 class="exec-info-title">
      <span class="exec-mode-pill"
            style="color:{meta.color}; background:{meta.bg}; border-color:{meta.border}">
        {meta.label}
      </span>
      mode is master-toggle only
    </h2>
    <p class="exec-info-body">
      {#if mode === 'paper'}Every broker-hitting action lands as a paper <code>AlgoOrder</code> row.{/if}
      {#if mode === 'shadow'}Orders are validated via Kite <code>basket_margin</code> but never placed.{/if}
      {#if mode === 'live'}Every action hits the real Kite broker.{/if}
      There is no dedicated workspace for this mode — the relevant surfaces live where the data lives.
    </p>
    <ul class="exec-info-links">
      <li><a href="/orders">/orders</a> — order log filtered to <code>mode={mode}</code></li>
      <li><a href="/dashboard">/dashboard</a> — P&amp;L analysis · summary grids · agent activity</li>
      <li><a href="/agents">/agents</a> — recent agent fires</li>
    </ul>
    <p class="exec-info-hint">
      Switch to <a href="/admin/execution?mode=sim">SIM</a> or
      <a href="/admin/execution?mode=replay">REPLAY</a> for a workspace.
    </p>
  </div>
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
  }
  .algo-ts {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    margin-left: auto;
  }
  .exec-info {
    padding: 0.85rem 1rem;
    background: linear-gradient(180deg, rgba(20,30,55,0.65) 0%, rgba(13,21,38,0.65) 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-left: 3px solid #fbbf24;
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    color: #c8d8f0;
  }
  .exec-info-title {
    font-size: 0.7rem;
    font-weight: 700;
    color: #fde68a;
    margin-bottom: 0.45rem;
    display: flex;
    align-items: center;
    gap: 0.45rem;
  }
  .exec-info-body { line-height: 1.4; margin-bottom: 0.6rem; }
  .exec-info-body code {
    color: #fde68a;
    background: rgba(251,191,36,0.10);
    padding: 0 0.25rem;
    border-radius: 2px;
  }
  .exec-info-links {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0;
  }
  .exec-info-links li { padding: 0.15rem 0; }
  .exec-info-links a { color: #7dd3fc; text-decoration: none; font-weight: 700; }
  .exec-info-links a:hover { color: #fbbf24; text-decoration: underline; }
  .exec-info-hint {
    font-style: italic;
    color: #7e97b8;
    margin-top: 0.6rem;
  }
  .exec-info-hint a { color: #fbbf24; text-decoration: underline; }
</style>
