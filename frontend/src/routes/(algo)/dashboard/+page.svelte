<script>
  import { onMount } from 'svelte';
  import PerformancePage from '$lib/PerformancePage.svelte';
  import { clientTimestamp, authStore } from '$lib/stores';
  import { fetchPaperStatus } from '$lib/api';

  let paperBranch = $state(/** @type {string|undefined} */ (undefined));
  let bannerDismissed = $state(false);

  const isDemo = $derived(
    !$authStore.user && paperBranch === 'main'
  );

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

<div class="page-header">
  <h1 class="algo-page-title">Dashboard</h1>
  <span class="algo-ts">{clientTimestamp()}</span>
</div>
<PerformancePage theme="ag-theme-algo" allowOrders={true} maskAccounts={false} compactHeader={true} enableOptionsLink={true} />

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
    border-bottom: 1px solid rgba(251,191,36,0.25);
    padding-bottom: 0.35rem;
    margin-bottom: 1rem;
  }

  /* Demo-mode onboarding banner — purple-tinted to match the DEMO
     navbar badge. One-line dismissible; stored in localStorage so it
     doesn't nag on every visit. */
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
  .demo-banner-text {
    color: #d8b4fe;
    flex: 1;
  }
  .demo-banner-text strong {
    color: #e9d5ff;
    font-weight: 700;
  }
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
