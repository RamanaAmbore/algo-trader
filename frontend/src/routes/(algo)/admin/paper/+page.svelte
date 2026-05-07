<script>
  // Paper-trading dashboard (/admin/paper).
  //
  // Mirrors the simulator page's layout but reads from the prod
  // PaperTradeEngine (mode 2 — real Kite quotes + paper trade engine on
  // main). Operators use this page to *visually* monitor paper-traded
  // agent fires: which contracts have an open chase, what the bid/ask
  // is doing tick-by-tick, and how the chase is progressing relative to
  // the limit price.
  //
  // The page is dev-friendly: on non-main branches the engine exists in
  // memory but no background tick_loop runs, so the page renders an
  // explanatory banner and disables polling. On main, polls every 3 s.

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, clientTimestamp, visibleInterval, branchLabel } from '$lib/stores';
  import {
    fetchPaperStatus, fetchChartSymbols, fetchChartBatch,
    fetchAlgoOrdersRecent,
  } from '$lib/api';
  import LogPanel   from '$lib/LogPanel.svelte';
  import PriceChart from '$lib/PriceChart.svelte';
  import InfoHint   from '$lib/InfoHint.svelte';
  import { priceFmt, qtyFmt } from '$lib/format';

  let status         = $state(/** @type {any} */ ({}));
  let orderRows      = $state(/** @type {any[]} */ ([]));
  let chartSymbols   = $state(/** @type {string[]} */ ([]));
  /** @type {Array<{symbol:string, kind:string, underlying:string|null}>} */
  let chartItems     = $state([]);
  // Batched chart payload — same pattern as the simulator page so 10
  // charts cost 1 round-trip per refresh.
  let chartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let error          = $state('');
  let loading        = $state(true);
  let logTab         = $state('order');
  let refreshTeardown;

  // Consecutive-failure counter — same idea as /admin/options. Hides
  // the error banner on a single transient hiccup (cold-start during
  // a redeploy, fresh wifi reconnect, etc.). Banner only surfaces
  // after 2+ in a row when no prior status to fall back on.
  let _loadFails = 0;
  async function load() {
    try {
      const [stat, syms, rows] = await Promise.all([
        fetchPaperStatus(),
        fetchChartSymbols('paper').catch(() => ({ items: [], symbols: [] })),
        fetchAlgoOrdersRecent(100, 'paper').catch(() => []),
      ]);
      status        = stat;
      chartSymbols  = syms?.symbols || [];
      chartItems    = syms?.items   || [];
      orderRows     = rows || [];
      // Batch the per-symbol fetches into one /charts/batch round-trip.
      // Skipped on dev (engine inactive — symbols list is empty anyway).
      if (chartSymbols.length) {
        try {
          const batch = await fetchChartBatch('paper', chartSymbols);
          const map = /** @type {Record<string, any>} */ ({});
          for (const c of (batch?.charts || [])) map[c.symbol] = c;
          chartsBySymbol = map;
        } catch (_) { /* charts fall back to self-poll */ }
      } else {
        chartsBySymbol = {};
      }
      error = '';
      _loadFails = 0;
    } catch (e) {
      _loadFails += 1;
      if (!status?.branch && _loadFails >= 2) {
        error = e.message;
      }
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    // Auth/redirect handled by the algo layout; demo visitors view
    // this page read-only.
    load();
    refreshTeardown = visibleInterval(load, 3000);
  });
  onDestroy(() => { refreshTeardown?.(); });

  const enabledClass = $derived(
    status?.enabled ? 'banner-active' : 'banner-disabled'
  );
</script>

<svelte:head><title>Paper Trading | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">Paper Trading</h1>
  <InfoHint text={'Visual dashboard for the prod paper-trade engine. Every broker-hitting action that hasn\'t been promoted to <span class="font-mono">execution.live</span> in <a class="text-[#7dd3fc] underline" href="/admin/settings">Settings</a> shows up here as an open chase order, and the chart panel tracks the bid/ask + underlying spot for the symbols involved. No real orders reach the broker.'} />
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

{#if error}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{error}</div>
{/if}

<!-- Status banner — green when the engine is actively chasing on main,
     amber when no orders are in flight, grey on dev (engine inactive). -->
<div class="paper-banner {enabledClass}" data-status={status?.enabled ? 'active' : 'inactive'}>
  {#if !status?.enabled}
    <span class="paper-banner-tag">DEV</span>
    <span>
      Paper engine is gated on this branch (<span class="font-mono">{branchLabel(status?.branch) || '?'}</span>).
      It exists in memory but no tick_loop is running. Promote your branch to
      <span class="font-mono">prod</span> to see live paper activity here.
    </span>
  {:else if (status?.open_order_count ?? 0) > 0}
    <span class="paper-banner-tag tag-active">CHASING</span>
    <span>
      <b>{status.open_order_count}</b>
      open paper order{status.open_order_count === 1 ? '' : 's'} on
      <span class="font-mono">{branchLabel(status.branch)}</span> ·
      {status.captured_symbols.length} symbol{status.captured_symbols.length === 1 ? '' : 's'}
      tracked, {status.captured_underlyings.length} underlying{status.captured_underlyings.length === 1 ? '' : 's'}
    </span>
  {:else}
    <span class="paper-banner-tag tag-idle">IDLE</span>
    <span>
      Paper engine is enabled on <span class="font-mono">{branchLabel(status?.branch)}</span>
      but no orders are currently in flight. Charts populate as soon as
      an agent fires a broker action.
    </span>
  {/if}
</div>

<!-- Open-order pills — same shape as the Simulator page's chase pills.
     One pill per in-flight chase showing side / qty / symbol / current
     limit / attempt count. -->
{#if status?.open_order_details?.length}
  <div class="paper-pills mb-3">
    <span class="paper-pills-label">Chasing ({status.open_order_details.length}):</span>
    {#each status.open_order_details as o}
      <span class="paper-pill">
        <span class="paper-pill-side paper-pill-side-{o.side === 'BUY' ? 'buy' : 'sell'}">{o.side}</span>
        <span class="paper-pill-sym">{o.symbol}</span>
        <span class="paper-pill-qty">{qtyFmt(o.qty)}</span>
        <span class="paper-pill-limit">@₹{priceFmt(o.limit_price)}</span>
        <span class="paper-pill-attempts">#{o.attempts}</span>
      </span>
    {/each}
  </div>
{/if}

<!-- Chart grid — one mini chart per symbol with captured ticks.
     Underlyings are rendered first (sky-blue SPOT tag), then derivatives
     grouped by their underlying. Derivative charts overlay the
     underlying spot as a dashed line. -->
{#if chartSymbols.length}
  <div class="paper-charts mb-3">
    {#each chartSymbols as sym (sym)}
      <PriceChart mode="paper" symbol={sym} height={170}
                  data={chartsBySymbol[sym]}
                  {chartsBySymbol} />
    {/each}
  </div>
{:else if !loading && status?.enabled}
  <div class="text-[0.65rem] text-[#7e97b8] mb-3 italic">
    No symbols with captured ticks yet. The chart panel populates as soon
    as the chase loop sees its first quote.
  </div>
{/if}

<LogPanel
  heightClass="h-[40vh]"
  initialTab={logTab}
  cmdHistory={[]}
  orderLog={[]}
  {orderRows}
  agentLog={[]}
  systemLog={[]}
  simLog={[]}
  onTabChange={(id) => { logTab = id; }}
/>

<style>
  /* Top status banner — subtle gradient + amber accent so the page looks
     of-a-piece with /admin/simulator. The data-status attribute drives
     the colour: active = sky (chasing) / amber (idle) / muted (dev). */
  .paper-banner {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    border: 1px solid rgba(251,191,36,0.2);
    border-left: 3px solid #fbbf24;
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    margin-bottom: 0.65rem;
    font-size: 0.65rem;
    color: #c8d8f0;
  }
  .paper-banner.banner-disabled {
    border-color: rgba(255,255,255,0.10);
    border-left-color: rgba(255,255,255,0.25);
    color: #7e97b8;
  }
  .paper-banner-tag {
    font-family: monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid currentColor;
    color: #7e97b8;
  }
  .paper-banner-tag.tag-active {
    color: #7dd3fc;
    background: rgba(125,211,252,0.10);
  }
  .paper-banner-tag.tag-idle {
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
  }

  /* Open-order pills — same look as the Simulator page so the two pages
     read as siblings. */
  .paper-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    align-items: center;
  }
  .paper-pills-label {
    font-family: monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    margin-right: 0.25rem;
  }
  .paper-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(125,211,252,0.3);
    background: rgba(125,211,252,0.08);
    font-family: monospace;
    font-size: 0.6rem;
  }
  .paper-pill-side {
    font-weight: 700;
    padding: 0 0.25rem;
    border-radius: 2px;
    font-size: 0.55rem;
  }
  .paper-pill-side-buy  { color: #4ade80; background: rgba(74,222,128,0.15); }
  .paper-pill-side-sell { color: #f87171; background: rgba(248,113,113,0.15); }
  .paper-pill-sym  { color: #c8d8f0; }
  .paper-pill-qty  { color: #fbbf24; font-weight: 700; }
  .paper-pill-limit { color: #7dd3fc; }
  .paper-pill-attempts {
    color: #fbbf24;
    font-weight: 700;
    border-left: 1px solid rgba(251,191,36,0.35);
    padding-left: 0.35rem;
    margin-left: 0.1rem;
  }

  /* Chart grid — same template as the Simulator page so charts are the
     same size on both surfaces. */
  .paper-charts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
  }
</style>
