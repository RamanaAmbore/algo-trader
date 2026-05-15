<script>
  import { onMount, getContext } from 'svelte';
  import MarketPulse from '$lib/MarketPulse.svelte';
  import PnlAnalysis from '$lib/PnlAnalysis.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import { clientTimestamp } from '$lib/stores';

  // ── Demo banner — sourced from the layout's shared context ─────────
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);
  let bannerDismissed = $state(false);

  onMount(() => {
    bannerDismissed = localStorage.getItem('ramboq.demo_banner_dismissed') === '1';
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
  <InfoHint popup text="Admin dashboard: real Kite holdings, positions, funds (account-scoped). P&amp;L Analysis below shows realised vs unrealised breakdown by symbol." />
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

<!-- Performance section — Funds + Positions Summary + Holdings Summary (no Symbols grid) -->
<MarketPulse
  title="Performance"
  enableWatchlists={false}
  enableSourceToggles={true}
  allowOrders={true}
  accountPicker={true}
  showSummary={true}
  showFunds={true}
  showSymbolsGrid={false} />

<!-- P&L Analysis section -->
<div class="mp-section-label pnl-section-label">P&amp;L Analysis</div>
<PnlAnalysis />

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

  /* P&L section label — matches MarketPulse's mp-section-label style */
  .mp-section-label {
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #fbbf24;
    margin-bottom: 0.25rem;
  }
  .pnl-section-label {
    margin-top: 0.75rem;
    margin-bottom: 0.3rem;
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
