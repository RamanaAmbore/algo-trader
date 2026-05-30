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
  import { authStore, nowStamp } from '$lib/stores';
  import OrderNotifications from '$lib/OrderNotifications.svelte';
  import AgentNotifications from '$lib/AgentNotifications.svelte';
  import InfoHint from '$lib/InfoHint.svelte';

  // Active tab — 'sim' or 'replay'. Seeded from ?tab= or the legacy
  // ?mode= (backward-compat for old SIM/REPLAY dropdown deep-links).
  let tab = $state(/** @type {'sim'|'replay'} */ ('sim'));

  // Panels are dynamic-imported so only the active tab's bundle lands
  // on first paint. Previously both SimulatorPanel + ReplayPanel were
  // top-level imports; even though only ONE renders at a time, both
  // bundles + every heavy dep they transitively pull (ag-Grid for
  // scenario tables, hand-rolled SVG chart libs, etc.) blocked the
  // first-paint of /admin/execution. Lazy-loading drops the initial
  // JS payload of this page by ~50% and lets the header render
  // immediately.
  let SimulatorPanel = $state(/** @type {any} */ (null));
  let ReplayPanel    = $state(/** @type {any} */ (null));

  function loadPanel(/** @type {'sim'|'replay'} */ t) {
    if (t === 'sim' && !SimulatorPanel) {
      import('$lib/execution/SimulatorPanel.svelte')
        .then(m => { SimulatorPanel = m.default; })
        .catch(err => console.warn('[Lab] SimulatorPanel load failed:', err));
    } else if (t === 'replay' && !ReplayPanel) {
      import('$lib/execution/ReplayPanel.svelte')
        .then(m => { ReplayPanel = m.default; })
        .catch(err => console.warn('[Lab] ReplayPanel load failed:', err));
    }
  }

  onMount(() => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    const params = page.url.searchParams;
    const want = params.get('tab') || params.get('mode');
    if (want === 'sim' || want === 'replay') tab = want;
    loadPanel(tab);  // kick off the active panel's bundle fetch
  });

  function pickTab(/** @type {'sim'|'replay'} */ t) {
    tab = t;
    loadPanel(t);
    const url = new URL(page.url);
    url.searchParams.set('tab', t);
    url.searchParams.delete('mode');
    goto(url.pathname + url.search, { replaceState: true, noScroll: true });
  }
</script>

<svelte:head><title>Lab | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Lab</h1>
    <InfoHint popup text="Research workspace for the two non-execution surfaces: <b>Scenario</b> (fabricated price moves on real positions — formerly Simulator) and <b>Backtest</b> (historical Kite OHLCV candles fed through the agent engine — formerly Replay). Your persistent master mode (LIVE/PAPER) lives in the navbar dropdown, separate from this page." />
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <OrderNotifications /><AgentNotifications />
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
  {#if SimulatorPanel}
    {@const Comp = SimulatorPanel}
    <Comp />
  {:else}
    <div class="lab-loading">Loading Scenario workspace…</div>
  {/if}
{:else if tab === 'replay'}
  {#if ReplayPanel}
    {@const Comp = ReplayPanel}
    <Comp />
  {:else}
    <div class="lab-loading">Loading Backtest workspace…</div>
  {/if}
{/if}

<style>
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
  /* Loading state shown for the brief moment between tab click and
     the lazy-imported panel bundle resolving. Subtle so it doesn't
     read as a real "loading spinner" — it's typically gone in 100ms. */
  .lab-loading {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    color: #7e97b8;
    padding: 1rem;
    text-align: center;
  }
</style>
