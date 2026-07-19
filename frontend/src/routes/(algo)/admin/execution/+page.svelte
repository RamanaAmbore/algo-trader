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
  import { authStore, lastRefreshAt } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';

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

  let _refreshing = $state(false);
  // SimulatorPanel / ReplayPanel don't yet expose a reload() handle so
  // we use a fixed-duration spinner — kicks off the next panel load and
  // flips the badge back after the panel's own debounce settles.
  function _onRefresh() {
    _refreshing = true;
    lastRefreshAt.set(Date.now());
    loadPanel(tab);
    setTimeout(() => { _refreshing = false; }, 400);
  }

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
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <!-- Page-header trio: Refresh + Order + Chart + Activity. The active
         panel (SimulatorPanel / ReplayPanel) reloads its own /api/sim/*
         status on its internal cadence; the page-level refresh bumps
         lastRefreshAt so the tooltip + connection badge stay in sync. -->
    <RefreshButton onClick={_onRefresh} loading={_refreshing} label="lab" />
    <PageHeaderActions />
  </span>
</div>

<AlgoTabs
  tabs={[
    { id: 'sim',    label: 'Scenario' },
    { id: 'replay', label: 'Backtest' },
  ]}
  bind:value={tab}
  onChange={pickTab}
/>
<div class="exec-tab-subtitle-row">
  {#if tab === 'sim'}
    <span class="exec-tab-subtitle">fabricated moves</span>
  {:else if tab === 'replay'}
    <span class="exec-tab-subtitle">historical data</span>
  {/if}
</div>

{#if tab === 'sim'}
  {#if SimulatorPanel}
    {@const Comp = SimulatorPanel}
    <Comp />
  {:else}
    <LoadingSkeleton variant="card" rows={5} />
  {/if}
{:else if tab === 'replay'}
  {#if ReplayPanel}
    {@const Comp = ReplayPanel}
    <Comp />
  {:else}
    <LoadingSkeleton variant="card" rows={5} />
  {/if}
{/if}

<style>
  /* Subtitle row shown below the AlgoTabs strip; describes the active
     tab's data source so operators know what "Scenario" vs "Backtest"
     means without hovering. */
  .exec-tab-subtitle-row {
    min-height: 1.1rem;
    margin-bottom: 0.55rem;
    padding-left: 0.15rem;
  }
  .exec-tab-subtitle {
    font-size: var(--fs-xs);
    font-weight: 500;
    color: var(--algo-muted);
    font-family: var(--font-numeric);
    letter-spacing: 0.02em;
  }
</style>
