<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import MarketPulse from '$lib/MarketPulse.svelte';
  import PnlAnalysis from '$lib/PnlAnalysis.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import { clientTimestamp, visibleInterval } from '$lib/stores';
  import { fetchPositions, fetchHoldings, fetchAgentEvents } from '$lib/api';
  import { priceFmt } from '$lib/format';

  // ── Demo banner — sourced from the layout's shared context ─────────
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);
  let bannerDismissed = $state(false);

  // ── Hero row: "what changed since I last looked?" ───────────────────
  // Compact strip above PnlAnalysis answering the executive question
  // first — total day P&L, agent fires today, open paper orders.
  let _todayPnl     = $state(/** @type {number|null} */ (null));
  let _firesToday   = $state(0);
  let _paperOpen    = $state(0);
  let _heroLoadedAt = $state(/** @type {string|null} */ (null));
  let _heroTeardown;

  async function loadHero() {
    try {
      const [positions, holdings, events] = await Promise.all([
        fetchPositions().catch(() => []),
        fetchHoldings().catch(() => []),
        fetchAgentEvents(50).catch(() => []),
      ]);
      // Sum day's P&L from positions (day-pnl) + holdings (day_change).
      let dayPnl = 0;
      for (const p of (positions || [])) dayPnl += Number(p.pnl) || 0;
      for (const h of (holdings || [])) {
        const dc = Number(h.day_change ?? h.day_change_pct_amount ?? 0);
        dayPnl += dc;
      }
      _todayPnl = dayPnl;
      // Agent fires today — count of events with kind=agent_fire, today (IST).
      const todayStart = new Date();
      todayStart.setHours(0, 0, 0, 0);
      _firesToday = (events || []).filter((e) => {
        const k = e.kind ?? e.event_type ?? '';
        if (k !== 'agent_fire') return false;
        const t = new Date(e.timestamp ?? e.created_at ?? 0);
        return t >= todayStart;
      }).length;
      // Open paper orders — read from layout's shared paperStatus.
      _paperOpen = Number(algoStatus.paperStatus?.open_order_count) || 0;
      _heroLoadedAt = clientTimestamp();
    } catch (_) { /* leave previous values up */ }
  }

  onMount(() => {
    bannerDismissed = localStorage.getItem('ramboq.demo_banner_dismissed') === '1';
    loadHero();
    _heroTeardown = visibleInterval(loadHero, 30000);
  });
  onDestroy(() => { _heroTeardown?.(); });

  function dismissBanner() {
    bannerDismissed = true;
    localStorage.setItem('ramboq.demo_banner_dismissed', '1');
  }

  const _pnlClass = $derived(
    _todayPnl == null ? 'hero-pnl-neutral'
    : _todayPnl > 0   ? 'hero-pnl-up'
    : _todayPnl < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );
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
  <InfoHint popup text="Admin dashboard: P&amp;L analysis first, then funds + position/holdings summary grids, then recent agent activity." />
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

<!-- Hero row — answers "what changed since I last looked?" before any
     detailed breakdowns. Three glanceable chips: today's total P&L,
     agent fires today, open paper orders. Refreshes every 30s on
     visible tab; pauses when backgrounded. -->
<div class="hero-row" role="status">
  <div class="hero-chip {_pnlClass}">
    <span class="hero-label">P&amp;L TODAY</span>
    <span class="hero-value">
      {#if _todayPnl == null}—{:else}{_todayPnl >= 0 ? '+' : ''}₹{priceFmt(_todayPnl)}{/if}
    </span>
  </div>
  <div class="hero-chip hero-chip-fires">
    <span class="hero-label">AGENT FIRES</span>
    <span class="hero-value">{_firesToday}</span>
    <span class="hero-meta">today</span>
  </div>
  <div class="hero-chip hero-chip-paper">
    <span class="hero-label">PAPER OPEN</span>
    <span class="hero-value">{_paperOpen}</span>
    <span class="hero-meta">orders</span>
  </div>
  {#if _heroLoadedAt}
    <span class="hero-refresh">refreshed {_heroLoadedAt}</span>
  {/if}
</div>

<!-- 1. P&L Analysis — realised + unrealised breakdown by symbol. Reads
     the live broker book, so it answers "where am I making/losing money"
     before showing aggregate balances below. -->
<div class="mp-section-label pnl-section-label">P&amp;L Analysis</div>
<PnlAnalysis />

<!-- 2. Summary grids — Funds + Positions Summary + Holdings Summary
     (Symbols grid intentionally off; the per-symbol detail view lives
     on /pulse and /performance). -->
<MarketPulse
  title="Performance"
  enableWatchlists={false}
  enableSourceToggles={true}
  allowOrders={true}
  accountPicker={true}
  showSummary={true}
  showFunds={true}
  showSymbolsGrid={false} />

<!-- 3. Agent activity — recent fires + action outcomes. Scoped to
     agent kinds so order events don't clutter the panel (those live
     on /orders). excludeSim filters out fabricated sim fires so the
     dashboard reflects only real-market activity even when an
     operator has a sim running in another tab. -->
<div class="mp-section-label pnl-section-label">Agent activity</div>
<UnifiedLog
  filter={{ kinds: ['agent_fire', 'agent_action_success', 'agent_action_error'] }}
  excludeSim={true}
  maxRows={30}
  emptyMessage="No agent fires yet today." />

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

  /* Section labels — demoted from amber to muted blue with amber
     uppercase tracking. Three-tier heading hierarchy: page title
     (amber bold), section label (muted blue uppercase), section
     heading inside MarketPulse (light blue). Without the demotion
     all three rendered in the same amber and the hierarchy collapsed
     visually. */
  .mp-section-label {
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #7e97b8;
    margin-bottom: 0.25rem;
  }

  /* Hero row — three glanceable chips with a "refreshed at" tail. */
  .hero-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.6rem;
    margin: 0 0 0.6rem 0;
    padding: 0.4rem 0.55rem;
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .hero-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 0.35rem;
    padding: 0.18rem 0.55rem;
    border-left: 2px solid;
    background: rgba(255,255,255,0.02);
    border-radius: 2px;
    font-family: ui-monospace, monospace;
    line-height: 1;
  }
  .hero-label {
    color: #7e97b8;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .hero-value {
    font-size: 0.82rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    color: #f1f7ff;
  }
  .hero-meta {
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
  }
  .hero-pnl-up    { border-left-color: #4ade80; }
  .hero-pnl-up    .hero-value { color: #4ade80; }
  .hero-pnl-down  { border-left-color: #f87171; }
  .hero-pnl-down  .hero-value { color: #f87171; }
  .hero-pnl-neutral { border-left-color: #7e97b8; }
  .hero-chip-fires  { border-left-color: #fbbf24; }
  .hero-chip-paper  { border-left-color: #7dd3fc; }
  .hero-refresh {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
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
