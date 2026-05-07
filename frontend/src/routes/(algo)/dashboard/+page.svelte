<script>
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import PerformancePage from '$lib/PerformancePage.svelte';
  import PnlPanel from '$lib/PnlPanel.svelte';
  import { clientTimestamp, authStore } from '$lib/stores';
  import { fetchPaperStatus } from '$lib/api';

  // ── Tab state — driven by ?tab= query param ────────────────────────
  /** @type {'performance' | 'pnl'} */
  let tab = $state(/** @type {'performance'|'pnl'} */ ('performance'));

  // Initialise from URL on mount; keep URL in sync on tab switch.
  onMount(() => {
    const qTab = page.url.searchParams.get('tab');
    if (qTab === 'pnl') tab = 'pnl';
  });

  function switchTab(/** @type {'performance'|'pnl'} */ t) {
    tab = t;
    const url = new URL(page.url);
    if (t === 'performance') {
      url.searchParams.delete('tab');
    } else {
      url.searchParams.set('tab', t);
    }
    goto(url.pathname + (url.search || ''), { replaceState: true, noScroll: true });
  }

  // ── Demo banner ────────────────────────────────────────────────────
  let paperBranch    = $state(/** @type {string|undefined} */ (undefined));
  let bannerDismissed = $state(false);

  const isDemo = $derived(!$authStore.user && paperBranch === 'main');

  onMount(async () => {
    bannerDismissed = localStorage.getItem('ramboq.demo_banner_dismissed') === '1';
    try {
      const s = await fetchPaperStatus();
      paperBranch = s?.branch;
    } catch (_) { /* treat as non-demo */ }
  });

  function dismissBanner() {
    bannerDismissed = true;
    localStorage.setItem('ramboq.demo_banner_dismissed', '1');
  }
</script>

<svelte:head>
  <title>Dashboard | RamboQuant Algo</title>
</svelte:head>

{#if isDemo && !bannerDismissed}
  <div class="demo-banner" role="status">
    <span class="demo-banner-text">
      You're viewing <strong>demo mode</strong> — real prod data with masked accounts.
      <a href="/signin" class="demo-banner-link">Sign in</a> for the full platform.
    </span>
    <button onclick={dismissBanner} class="demo-banner-close" aria-label="Dismiss">×</button>
  </div>
{/if}

<!-- Page header -->
<div class="page-header">
  <h1 class="algo-page-title">Dashboard</h1>
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

<!-- Tab strip — sits below the page header, above the panel. -->
<div class="dash-tabs-row">
  <div class="dash-tabs">
    <button type="button"
            class="dash-tab"
            class:dash-tab-active={tab === 'performance'}
            onclick={() => switchTab('performance')}>
      Performance
    </button>
    <button type="button"
            class="dash-tab"
            class:dash-tab-active={tab === 'pnl'}
            onclick={() => switchTab('pnl')}>
      P&amp;L
    </button>
  </div>
</div>

<!-- Panel area -->
{#if tab === 'performance'}
  <PerformancePage theme="ag-theme-algo" allowOrders={true} maskAccounts={false} compactHeader={true} enableOptionsLink={true} />
{:else}
  <PnlPanel active={tab === 'pnl'} />
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
    margin-bottom: 0.3rem;
  }

  /* ── Tab strip ──────────────────────────────────────────────────── */
  .dash-tabs-row {
    display: flex;
    align-items: flex-end;
    border-bottom: 1px solid rgba(255,255,255,0.10);
    margin-bottom: 0.75rem;
  }
  .dash-tabs {
    display: flex;
    gap: 0;
  }
  .dash-tab {
    padding: 0.3rem 0.9rem 0.3rem;
    font-size: 0.68rem;
    font-weight: 600;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
    color: #7e97b8;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    cursor: pointer;
    transition: color 0.1s, border-color 0.1s;
    white-space: nowrap;
    outline: none;
  }
  .dash-tab:hover { color: #c8d8f0; }
  .dash-tab-active {
    color: #fbbf24;
    border-bottom-color: #d4920c;
    font-weight: 700;
  }

  /* Demo-mode onboarding banner — purple-tinted to match the DEMO
     navbar badge. One-line dismissible; stored in localStorage. */
  .demo-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.45rem 0.75rem;
    margin-bottom: 0.75rem;
    border-radius: 4px;
    background: rgba(168,85,247,0.15);
    border: 1px solid rgba(168,85,247,0.35);
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
  }
  .demo-banner-text { color: #d8b4fe; flex: 1; }
  .demo-banner-text strong { color: #e9d5ff; font-weight: 700; }
  .demo-banner-link {
    color: #c084fc;
    text-decoration: underline;
    text-underline-offset: 2px;
    font-weight: 600;
  }
  .demo-banner-link:hover { color: #e9d5ff; }
  .demo-banner-close {
    flex-shrink: 0;
    background: none;
    border: none;
    color: rgba(168,85,247,0.6);
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    padding: 0 0.15rem;
    transition: color 0.1s;
  }
  .demo-banner-close:hover { color: #c084fc; }
</style>
