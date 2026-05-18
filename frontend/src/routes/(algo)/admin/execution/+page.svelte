<script>
  // Lab workspace (URL: /admin/execution kept for backward-compat).
  //
  // Two tabs:
  //   [Scenario] [Backtest]
  // - Scenario: fabricated price moves on real positions
  //   (formerly "Simulator").
  // - Backtest: historical-data backtest using real Kite OHLCV
  //   candles (formerly "Replay"). "Replay" is now reserved for
  //   "re-run a past iteration deterministically" only.
  //
  // The master-mode chip (LIVE/PAPER/SHADOW) is intentionally NOT
  // shown on this page — it lives in the navbar dropdown only.
  // Lab is a research workspace; surfacing the master mode here
  // confused operators ("am I doing LIVE trades?" — no, you're
  // running a sandbox).

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { authStore, clientTimestamp } from '$lib/stores';
  import InfoHint       from '$lib/InfoHint.svelte';
  import SimulatorPanel from '$lib/execution/SimulatorPanel.svelte';
  import ReplayPanel    from '$lib/execution/ReplayPanel.svelte';

  // Active tab — 'sim' or 'replay'. Seeded from ?tab= or the legacy
  // ?mode= (backward-compat for old SIM/REPLAY dropdown deep-links).
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
    const url = new URL(page.url);
    url.searchParams.set('tab', t);
    url.searchParams.delete('mode');
    goto(url.pathname + url.search, { replaceState: true, noScroll: true });
  }
</script>

<svelte:head><title>Lab | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <div class="exec-header-left">
    <h1 class="algo-page-title">Lab</h1>
    <InfoHint popup text="Research workspace for the two non-execution surfaces: <b>Scenario</b> (fabricated price moves on real positions — formerly Simulator) and <b>Backtest</b> (historical Kite OHLCV candles fed through the agent engine — formerly Replay). Your persistent master mode (LIVE/PAPER) lives in the navbar dropdown, separate from this page." />
  </div>
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

<div class="exec-tabs" role="tablist" aria-label="Lab workspace">
  <button class="exec-tab" class:exec-tab-active={tab === 'sim'}
          role="tab" aria-selected={tab === 'sim'} onclick={() => pickTab('sim')}>
    Scenario
    <span class="exec-tab-subtitle">fabricated moves</span>
  </button>
  <button class="exec-tab" class:exec-tab-active={tab === 'replay'}
          role="tab" aria-selected={tab === 'replay'} onclick={() => pickTab('replay')}>
    Backtest
    <span class="exec-tab-subtitle">historical data</span>
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
  .algo-ts {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    margin-left: auto;
  }
  /* Tab strip — [Scenario] [Backtest]. Active tab carries an amber
     bottom border so the visual reads as a section header for the
     workspace below. */
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
