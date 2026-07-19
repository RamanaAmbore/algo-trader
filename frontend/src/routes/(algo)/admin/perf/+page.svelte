<!--
  /admin/perf — per-page / per-route perf-snapshot dashboard.

  Data flow:
    - /api/admin/perf/latest  → one row per (side, page_or_route) → card grid
    - /api/admin/perf/history?page=<X>&days=30 → per-card sparkline (fetched
      lazily when the latest list arrives)
    - /api/admin/perf/regressions?days=7&threshold_pct=10 → top banner

  No live polling — snapshots update once per night at 04:00 IST.
  RefreshButton forces a re-fetch.

  Layout:
    Top:    Regressions banner (empty-state or list)
    Middle: Frontend card grid (top 8 FE pages by LOC, desc)
    Bottom: Backend card grid (top 8 BE routes by LOC, desc)
    Each card: header (name + latest LOC / cc_max / cc_avg) + two 60px SVG
    line charts (cc_max 30-day, LCP or route_p95_ms 30-day) + regression badge.

  Colors per project convention:
    --c-info   (#22d3ee) for LOC sparkline area
    --c-action (#fbbf24) for cc_max line
    --c-long   (#4ade80) for LCP/p95 under-budget
    --c-short  (#f87171) for LCP/p95 over-budget
-->
<script>
  import { onMount, untrack } from 'svelte';
  import { nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';
  import {
    fetchPerfLatest,
    fetchPerfHistory,
    fetchPerfRegressions,
  } from '$lib/api';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { userRole } from '$lib/rbac';
  import InfoHint from '$lib/InfoHint.svelte';
  import { METRIC_META } from '$lib/data/metricMetadata.js';

  // ── State ─────────────────────────────────────────────────────────────
  /** @type {Array<any>} */
  let latestRows  = $state([]);
  /** @type {Array<any>} */
  let regressions = $state([]);

  /** @type {Record<string, Array<any>>} */
  let historyData = $state({});

  let _showLiveTs = $state(false);
  let loadingLatest      = $state(false);
  let loadingRegressions = $state(false);
  let loadingHistory     = $state(false);
  let error              = $state('');

  // ── Derived views ─────────────────────────────────────────────────────
  /** Top 8 FE pages by LOC desc. */
  const feCards = $derived(
    latestRows
      .filter(r => r.side === 'FE')
      .sort((a, b) => (b.loc ?? 0) - (a.loc ?? 0))
      .slice(0, 8)
  );

  /** Top 8 BE routes by LOC desc. */
  const beCards = $derived(
    latestRows
      .filter(r => r.side === 'BE')
      .sort((a, b) => (b.loc ?? 0) - (a.loc ?? 0))
      .slice(0, 8)
  );

  /** True when we have ≥1 snapshot row. */
  const hasData = $derived(latestRows.length > 0);

  /** O(1) worst-regression lookup per page name, keyed during render. */
  const regressionMap = $derived(
    regressions.reduce((m, r) => {
      const e = m.get(r.page);
      if (!e || r.delta_pct > e.delta_pct) m.set(r.page, r);
      return m;
    }, /** @type {Map<string,any>} */ (new Map()))
  );

  // ── Data loading ──────────────────────────────────────────────────────
  async function loadLatest() {
    loadingLatest = true;
    error = '';
    try {
      const r = await fetchPerfLatest();
      latestRows = Array.isArray(r?.rows) ? r.rows : [];
    } catch (e) {
      error = e?.message || 'Perf-snapshot fetch failed';
      toast.error(`Perf load failed: ${e?.message || 'unknown'}`);
    } finally {
      loadingLatest = false;
    }
  }

  async function loadRegressions() {
    loadingRegressions = true;
    try {
      const r = await fetchPerfRegressions(7, 10);
      regressions = Array.isArray(r?.regressions) ? r.regressions : [];
    } catch (e) {
      regressions = [];
    } finally {
      loadingRegressions = false;
    }
  }

  async function loadHistory(cards) {
    if (!cards.length) return;
    loadingHistory = true;
    const next = { ...untrack(() => historyData) };
    const missing = cards.filter(c => !next[c.page_or_route]);
    if (missing.length) {
      const results = await Promise.allSettled(
        missing.map(c => fetchPerfHistory(c.page_or_route, 30))
      );
      for (let i = 0; i < missing.length; i++) {
        const r = results[i];
        next[missing[i].page_or_route] = r.status === 'fulfilled' && Array.isArray(r.value?.rows)
          ? r.value.rows
          : [];
      }
    }
    historyData = next;
    loadingHistory = false;
  }

  async function refresh() {
    historyData = {};
    await Promise.all([loadLatest(), loadRegressions()]);
    const all = [...feCards, ...beCards];
    if (all.length) loadHistory(all);
  }

  onMount(async () => {
    await refresh();
  });

  // ── SVG helpers (same approach as /admin/metrics) ─────────────────────
  /**
   * Build SVG path + area for a sparkline from an array of values.
   * Null values break the line without dropping everything to 0.
   */
  function sparkPath(
    /** @type {Array<number|null>} */ vals,
    w = 200,
    h = 60,
  ) {
    if (!vals || vals.length === 0) {
      return { path: '', area: '', dots: [], yMin: 0, yMax: 1 };
    }
    const nonNull = vals.filter(v => v !== null && v !== undefined);
    if (nonNull.length === 0) {
      return { path: '', area: '', dots: [], yMin: 0, yMax: 1 };
    }
    const yMin = Math.min(...nonNull);
    const yMax = Math.max(...nonNull);
    const span  = yMax === yMin ? 1 : yMax - yMin;
    const n     = vals.length;
    const xStep = n <= 1 ? 0 : w / (n - 1);

    let path = '';
    let area = '';
    let lastValid = false;
    const dots = [];

    for (let i = 0; i < n; i++) {
      const v = vals[i];
      if (v === null || v === undefined) { lastValid = false; continue; }
      const x = i * xStep;
      const y = h - ((v - yMin) / span) * h * 0.9 - h * 0.05;
      if (!lastValid) {
        path += `M${x.toFixed(1)},${y.toFixed(1)}`;
        if (area === '') area = `M${x.toFixed(1)},${h}L${x.toFixed(1)},${y.toFixed(1)}`;
      } else {
        path += ` L${x.toFixed(1)},${y.toFixed(1)}`;
        area += ` L${x.toFixed(1)},${y.toFixed(1)}`;
      }
      lastValid = true;
      dots.push({ x, y, value: v });
    }
    if (dots.length > 0) {
      area += ` L${dots[dots.length - 1].x.toFixed(1)},${h}Z`;
    }
    return { path, area, dots, yMin, yMax };
  }

  /** Extract a numeric metric series from a history row array. */
  function series(
    /** @type {Array<any>} */ rows,
    /** @type {string} */ col,
  ) {
    return rows.map(r => {
      const v = r[col];
      return (v === null || v === undefined) ? null : Number(v);
    });
  }

  function latestVal(/** @type {Array<any>} */ rows, /** @type {string} */ col) {
    for (let i = rows.length - 1; i >= 0; i--) {
      const v = rows[i][col];
      if (v !== null && v !== undefined) return Number(v);
    }
    return null;
  }

  // ── Formatting helpers ────────────────────────────────────────────────
  function fmtNum(/** @type {number|null|undefined} */ n) {
    if (n === null || n === undefined) return '—';
    if (!Number.isFinite(n)) return '—';
    if (Number.isInteger(n)) return n.toLocaleString('en-IN');
    return n.toFixed(2);
  }

  function fmtTs(/** @type {string} */ iso) {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        month: 'short', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
        hour12: false,
      }) + ' IST';
    } catch { return iso; }
  }

  /** Short name for display: strip leading '/api/admin/' or '/api/', show last 2 segments. */
  function shortName(/** @type {string} */ name) {
    return name
      .replace(/^\/api\/admin\//, '')
      .replace(/^\/api\//, '')
      .replace(/^lib::/, '')
      .replace(/^GET /, '')
      .replace(/^POST /, '');
  }

  /**
   * Regression badge for a card.
   * Returns null | 'amber' | 'red' and the worst delta_pct string.
   */
  function cardBadge(/** @type {string} */ pageName) {
    const worst = regressionMap.get(pageName);
    if (!worst) return null;
    return { color: worst.delta_pct > 25 ? 'red' : 'amber', delta_pct: worst.delta_pct, metric: worst.metric };
  }

  /** For the LCP / route_p95_ms chart color: are we over a threshold? */
  const LCP_BUDGET_MS  = 2500;
  const P95_BUDGET_MS  = 300;

  function latencyColor(
    /** @type {number|null} */ val,
    /** @type {boolean} */ isFE,
  ) {
    if (val === null || val === undefined) return 'var(--c-info)';
    const budget = isFE ? LCP_BUDGET_MS : P95_BUDGET_MS;
    return val > budget ? 'var(--c-short)' : 'var(--c-long)';
  }
</script>

<svelte:head>
  <title>Perf Dashboard · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Perf Dashboard</h1>
  </span>
  <span class="algo-ts-group" onclick={() => { if ($lastRefreshAt) _showLiveTs = !_showLiveTs; }} onkeydown={(e) => { if ($lastRefreshAt && (e.key === "Enter" || e.key === " ")) _showLiveTs = !_showLiveTs; }} role="button" tabindex="0">
    <span class="algo-ts"
          class:algo-ts-hidden={!!$lastRefreshAt && _showLiveTs}
          title={$lastRefreshAt ? 'Live clock — tap to switch' : 'Live clock'}>
      {$nowStamp}
    </span>
    {#if $lastRefreshAt}
      <span class="algo-ts-vsep" aria-hidden="true">|</span>
      <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}>
        {formatDualTz($lastRefreshAt)}
      </span>
    {/if}
  </span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton
      onClick={refresh}
      loading={loadingLatest || loadingRegressions}
      label="perf snapshots"
    />
    <PageHeaderActions />
  </span>
</div>

{#if error}
  <EmptyState title="Could not load perf snapshots" hint={error} icon="warn" />
{:else if loadingLatest && !hasData}
  <LoadingSkeleton variant="card" rows={4} />
{:else if !hasData}
  <!-- First-deploy empty state -->
  <EmptyState
    title="No perf snapshots yet"
    icon="chart"
  >
    {#snippet hintBody()}
      Snapshots start populating tonight at 04:00 IST.<br>
      Check back tomorrow, or run<br>
      <code>python scripts/perf_baseline.py</code> manually.
    {/snippet}
  </EmptyState>
{:else}

  <!-- ── Regressions banner ─────────────────────────────────────────── -->
  <section class="perf-section">
    <h2 class="perf-h2">Regressions (last 7 days)</h2>
    {#if loadingRegressions}
      <LoadingSkeleton variant="block" rows={2} />
    {:else if regressions.length === 0}
      <p class="perf-no-regressions" role="status">
        No regressions detected in the last 7 days.
      </p>
    {:else}
      <div class="perf-regression-list" role="list">
        {#each regressions as reg}
          {@const badge = reg.delta_pct > 25 ? 'red' : 'amber'}
          <div class="perf-regression-row" role="listitem">
            <span class="perf-reg-badge perf-reg-badge--{badge}"
                  class:perf-reg-badge--red={badge === 'red'}
                  class:perf-reg-badge--amber={badge === 'amber'}>
              +{reg.delta_pct}%
            </span>
            <span class="perf-reg-page">{shortName(reg.page)}</span>
            <span class="perf-reg-metric">{reg.metric}</span>
            <span class="perf-reg-nums" aria-label="current vs median">
              {fmtNum(reg.current)} vs {fmtNum(reg.median)}
            </span>
          </div>
        {/each}
      </div>
    {/if}
  </section>

  <!-- ── Frontend card grid ────────────────────────────────────────── -->
  {#if feCards.length > 0}
    <section class="perf-section">
      <h2 class="perf-h2">Frontend pages (top {feCards.length} by LOC)</h2>
      <div class="perf-card-grid">
        {#each feCards as card}
          {@const hist = historyData[card.page_or_route] ?? []}
          {@const ccSeries  = sparkPath(series(hist, 'cc_max'),  200, 60)}
          {@const lcpSeries = sparkPath(series(hist, 'lcp_ms'),  200, 60)}
          {@const badge     = cardBadge(card.page_or_route)}
          {@const latLcp    = latestVal(hist, 'lcp_ms') ?? card.lcp_ms}
          {@const lcpColor  = latencyColor(latLcp, true)}
          <article
            class="perf-card"
            class:perf-card--regressed={badge !== null}
            aria-label="Page metrics: {shortName(card.page_or_route)}"
          >
            <div class="perf-card-head">
              <span class="perf-card-name" title={card.page_or_route}>
                {shortName(card.page_or_route)}
              </span>
              {#if badge}
                <span
                  class="perf-card-badge"
                  class:perf-card-badge--amber={badge.color === 'amber'}
                  class:perf-card-badge--red={badge.color === 'red'}
                  title="{badge.metric} +{badge.delta_pct}%"
                >
                  {badge.color === 'red' ? 'REGRESSED' : 'WATCH'}
                </span>
              {/if}
            </div>

            <!-- Headline stats row -->
            <div class="perf-stats">
              <span class="perf-stat">
                <span class="perf-stat-label"><span class="metric-label">LOC<InfoHint content={METRIC_META.loc} popup={true} maxWidth="26rem" /></span></span>
                <span class="perf-stat-val" style="color:var(--c-info)">{fmtNum(card.loc)}</span>
              </span>
              <span class="perf-stat">
                <span class="perf-stat-label"><span class="metric-label">cc max<InfoHint content={METRIC_META.cc_max} popup={true} maxWidth="26rem" /></span></span>
                <span class="perf-stat-val" style="color:var(--c-action)">{fmtNum(card.cc_max)}</span>
              </span>
              <span class="perf-stat">
                <span class="perf-stat-label"><span class="metric-label">cc avg<InfoHint content={METRIC_META.cc_avg} popup={true} maxWidth="26rem" /></span></span>
                <span class="perf-stat-val" style="color:var(--c-action)">{fmtNum(card.cc_avg)}</span>
              </span>
              {#if card.lcp_ms !== null && card.lcp_ms !== undefined}
                <span class="perf-stat">
                  <span class="perf-stat-label"><span class="metric-label">LCP<InfoHint content={METRIC_META.lcp_ms} popup={true} maxWidth="26rem" /></span></span>
                  <span class="perf-stat-val" style="color:{latencyColor(card.lcp_ms, true)}">{fmtNum(card.lcp_ms)}ms</span>
                </span>
              {/if}
            </div>

            <!-- Chart row: cc_max trend | LCP trend -->
            <div class="perf-charts">
              <div class="perf-chart-wrap">
                <div class="perf-chart-label">cc_max (30d)</div>
                <svg viewBox="0 0 200 60" preserveAspectRatio="none"
                     class="perf-chart-svg" aria-hidden="true">
                  {#if ccSeries.area}
                    <path d={ccSeries.area} class="perf-area-action" />
                  {/if}
                  {#if ccSeries.path}
                    <path d={ccSeries.path} class="perf-line-action" />
                  {/if}
                </svg>
              </div>
              <div class="perf-chart-wrap">
                <div class="perf-chart-label">LCP ms (30d)</div>
                <svg viewBox="0 0 200 60" preserveAspectRatio="none"
                     class="perf-chart-svg" aria-hidden="true">
                  {#if lcpSeries.area}
                    <path d={lcpSeries.area}
                          style="fill: color-mix(in srgb, {lcpColor} 18%, transparent);" />
                  {/if}
                  {#if lcpSeries.path}
                    <path d={lcpSeries.path}
                          style="fill:none;stroke:{lcpColor};stroke-width:1.4;" />
                  {/if}
                </svg>
              </div>
            </div>

            <!-- Hotspots (if any) -->
            {#if card.hotspots_json?.length}
              <div class="perf-hotspot-mini">
                {#each card.hotspots_json.slice(0, 3) as h}
                  <span class="perf-hotspot-chip" title="cc={h.cc} line={h.line}">
                    {h.fn_name ?? h.fn ?? '?'}  <em>{h.cc}</em>
                  </span>
                {/each}
              </div>
            {/if}

            <div class="perf-card-foot">
              as of {fmtTs(card.captured_at)}
            </div>
          </article>
        {/each}
      </div>
    </section>
  {/if}

  <!-- ── Backend route card grid ───────────────────────────────────── -->
  {#if beCards.length > 0}
    <section class="perf-section">
      <h2 class="perf-h2">Backend routes (top {beCards.length} by LOC)</h2>
      <div class="perf-card-grid">
        {#each beCards as card}
          {@const hist    = historyData[card.page_or_route] ?? []}
          {@const ccSeries = sparkPath(series(hist, 'cc_max'),  200, 60)}
          {@const p95Series = sparkPath(series(hist, 'route_p95_ms'), 200, 60)}
          {@const badge    = cardBadge(card.page_or_route)}
          {@const latP95   = latestVal(hist, 'route_p95_ms') ?? card.route_p95_ms}
          {@const p95Color = latencyColor(latP95, false)}
          <article
            class="perf-card"
            class:perf-card--regressed={badge !== null}
            aria-label="Route metrics: {shortName(card.page_or_route)}"
          >
            <div class="perf-card-head">
              <span class="perf-card-name" title={card.page_or_route}>
                {shortName(card.page_or_route)}
              </span>
              {#if badge}
                <span
                  class="perf-card-badge"
                  class:perf-card-badge--amber={badge.color === 'amber'}
                  class:perf-card-badge--red={badge.color === 'red'}
                  title="{badge.metric} +{badge.delta_pct}%"
                >
                  {badge.color === 'red' ? 'REGRESSED' : 'WATCH'}
                </span>
              {/if}
            </div>

            <div class="perf-stats">
              <span class="perf-stat">
                <span class="perf-stat-label"><span class="metric-label">LOC<InfoHint content={METRIC_META.loc} popup={true} maxWidth="26rem" /></span></span>
                <span class="perf-stat-val" style="color:var(--c-info)">{fmtNum(card.loc)}</span>
              </span>
              <span class="perf-stat">
                <span class="perf-stat-label"><span class="metric-label">cc max<InfoHint content={METRIC_META.cc_max} popup={true} maxWidth="26rem" /></span></span>
                <span class="perf-stat-val" style="color:var(--c-action)">{fmtNum(card.cc_max)}</span>
              </span>
              <span class="perf-stat">
                <span class="perf-stat-label"><span class="metric-label">cc avg<InfoHint content={METRIC_META.cc_avg} popup={true} maxWidth="26rem" /></span></span>
                <span class="perf-stat-val" style="color:var(--c-action)">{fmtNum(card.cc_avg)}</span>
              </span>
              {#if card.route_p95_ms !== null && card.route_p95_ms !== undefined}
                <span class="perf-stat">
                  <span class="perf-stat-label"><span class="metric-label">p95<InfoHint content={METRIC_META.route_p95_ms} popup={true} maxWidth="26rem" /></span></span>
                  <span class="perf-stat-val" style="color:{latencyColor(card.route_p95_ms, false)}">{fmtNum(card.route_p95_ms)}ms</span>
                </span>
              {/if}
            </div>

            <div class="perf-charts">
              <div class="perf-chart-wrap">
                <div class="perf-chart-label">cc_max (30d)</div>
                <svg viewBox="0 0 200 60" preserveAspectRatio="none"
                     class="perf-chart-svg" aria-hidden="true">
                  {#if ccSeries.area}
                    <path d={ccSeries.area} class="perf-area-action" />
                  {/if}
                  {#if ccSeries.path}
                    <path d={ccSeries.path} class="perf-line-action" />
                  {/if}
                </svg>
              </div>
              <div class="perf-chart-wrap">
                <div class="perf-chart-label">p95 ms (30d)</div>
                <svg viewBox="0 0 200 60" preserveAspectRatio="none"
                     class="perf-chart-svg" aria-hidden="true">
                  {#if p95Series.area}
                    <path d={p95Series.area}
                          style="fill: color-mix(in srgb, {p95Color} 18%, transparent);" />
                  {/if}
                  {#if p95Series.path}
                    <path d={p95Series.path}
                          style="fill:none;stroke:{p95Color};stroke-width:1.4;" />
                  {/if}
                </svg>
              </div>
            </div>

            {#if card.hotspots_json?.length}
              <div class="perf-hotspot-mini">
                {#each card.hotspots_json.slice(0, 3) as h}
                  <span class="perf-hotspot-chip" title="cc={h.cc} line={h.line}">
                    {h.fn_name ?? h.fn ?? '?'}  <em>{h.cc}</em>
                  </span>
                {/each}
              </div>
            {/if}

            <div class="perf-card-foot">
              as of {fmtTs(card.captured_at)}
            </div>
          </article>
        {/each}
      </div>
    </section>
  {/if}

  <!-- ── Top 10 hotspots panel ─────────────────────────────────────── -->
  {@const allHotspots = latestRows
    .filter(r => r.hotspots_json?.length)
    .flatMap(r => (r.hotspots_json ?? []).map(h => ({
      ...h,
      page: r.page_or_route,
      side: r.side,
      card_cc_max: r.cc_max,
    })))
    .sort((a, b) => (b.cc ?? 0) - (a.cc ?? 0))
    .slice(0, 10)
  }
  {#if allHotspots.length > 0}
    <section class="perf-section">
      <h2 class="perf-h2">Top 10 hotspots (by cyclomatic complexity)</h2>
      <table class="perf-hotspot-table" aria-label="Hotspot functions">
        <thead>
          <tr>
            <th>Function</th>
            <th>Page / route</th>
            <th class="num"><span class="metric-label">cc<InfoHint content={METRIC_META.hotspot_cc} popup={true} maxWidth="26rem" /></span></th>
            <th class="num">line</th>
          </tr>
        </thead>
        <tbody>
          {#each allHotspots as h}
            <tr>
              <td class="perf-fn-name">{h.fn_name ?? h.fn ?? '—'}</td>
              <td class="perf-fn-page" title={h.page}>{shortName(h.page)}</td>
              <td class="num perf-fn-cc">{h.cc ?? '—'}</td>
              <td class="num perf-fn-line">{h.line ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  {/if}

{/if}

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  /* ── Section headings ─────────────────────────────────────────────── */
  .perf-section {
    margin: 0.8rem 0 1.4rem;
  }
  .perf-h2 {
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin: 0 0 0.5rem;
    color: var(--c-action);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }

  /* ── Regression list ──────────────────────────────────────────────── */
  .perf-no-regressions {
    font-size: var(--fs-lg);
    color: var(--algo-muted, #94a3b8);
    font-style: italic;
    margin: 0;
  }
  .perf-regression-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .perf-regression-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: var(--fs-lg);
    padding: 0.22rem 0.4rem;
    border-radius: 4px;
    background: rgba(148, 163, 184, 0.05);
    flex-wrap: wrap;
  }
  .perf-reg-badge {
    font-size: var(--fs-md);
    font-weight: 700;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .perf-reg-badge--amber {
    background: rgba(251, 191, 36, 0.14);
    border: 1px solid rgba(251, 191, 36, 0.45);
    color: var(--c-action);
  }
  .perf-reg-badge--red {
    background: rgba(248, 113, 113, 0.14);
    border: 1px solid rgba(248, 113, 113, 0.45);
    color: var(--c-short);
  }
  .perf-reg-page {
    font-weight: 600;
    color: var(--text, #e2e8f0);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-lg);
  }
  .perf-reg-metric {
    color: var(--text-soft, #94a3b8);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-md);
  }
  .perf-reg-nums {
    margin-left: auto;
    font-variant-numeric: tabular-nums;
    color: var(--text-soft, #94a3b8);
    font-size: var(--fs-md);
    white-space: nowrap;
  }

  /* ── Card grid ────────────────────────────────────────────────────── */
  .perf-card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 0.6rem;
  }
  @media (max-width: 600px) {
    .perf-card-grid {
      grid-template-columns: 1fr;
    }
  }
  .perf-card {
    background: rgba(34, 211, 238, 0.05);
    border: 1px solid rgba(34, 211, 238, 0.22);
    border-radius: 6px;
    padding: 0.5rem 0.6rem 0.4rem;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .perf-card--regressed {
    border-color: rgba(251, 191, 36, 0.35);
    background: rgba(251, 191, 36, 0.04);
  }

  /* Card header */
  .perf-card-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.4rem;
  }
  .perf-card-name {
    font-size: var(--fs-lg);
    font-weight: 600;
    color: var(--text, #e2e8f0);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
  .perf-card-badge {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 0.08rem 0.38rem;
    border-radius: 3px;
    flex-shrink: 0;
    cursor: default;
  }
  .perf-card-badge--amber {
    background: rgba(251, 191, 36, 0.14);
    border: 1px solid rgba(251, 191, 36, 0.45);
    color: var(--c-action);
  }
  .perf-card-badge--red {
    background: rgba(248, 113, 113, 0.14);
    border: 1px solid rgba(248, 113, 113, 0.45);
    color: var(--c-short);
  }

  /* Stats row */
  .perf-stats {
    display: flex;
    gap: 0.7rem;
    flex-wrap: wrap;
  }
  .perf-stat {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
  }
  .perf-stat-label {
    font-size: var(--fs-xs);
    color: var(--text-soft, #94a3b8);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    line-height: 1;
    margin-bottom: 0.1rem;
  }
  /* Inline metric label + InfoHint chip. */
  .metric-label {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    white-space: nowrap;
  }
  .perf-stat-val {
    font-size: var(--fs-lg);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    line-height: 1.2;
  }

  /* Charts side by side */
  .perf-charts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.4rem;
    margin-top: 0.15rem;
  }
  .perf-chart-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .perf-chart-label {
    font-size: var(--fs-xs);
    color: var(--text-soft, #94a3b8);
    font-variant-numeric: tabular-nums;
  }
  .perf-chart-svg {
    width: 100%;
    height: 60px;
    display: block;
  }

  /* Shared SVG class for action/amber cc_max */
  :global(.perf-area-action) {
    fill: rgba(251, 191, 36, 0.14);
    stroke: none;
  }
  :global(.perf-line-action) {
    fill: none;
    stroke: var(--c-action);
    stroke-width: 1.4;
  }

  /* Hotspot mini list inside a card */
  .perf-hotspot-mini {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    margin-top: 0.1rem;
  }
  .perf-hotspot-chip {
    font-size: var(--fs-xs);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    color: var(--text-soft, #94a3b8);
    background: rgba(148, 163, 184, 0.08);
    border: 1px solid rgba(148, 163, 184, 0.15);
    border-radius: 3px;
    padding: 0.05rem 0.32rem;
    white-space: nowrap;
    overflow: hidden;
    max-width: 12rem;
    text-overflow: ellipsis;
  }
  .perf-hotspot-chip em {
    font-style: normal;
    color: var(--c-action);
    font-weight: 700;
    margin-left: 0.25rem;
  }

  .perf-card-foot {
    font-size: var(--fs-xs);
    color: var(--text-soft, #94a3b8);
    margin-top: 0.15rem;
    font-variant-numeric: tabular-nums;
  }

  /* ── Hotspot table ────────────────────────────────────────────────── */
  .perf-hotspot-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-xl);
    font-variant-numeric: tabular-nums;
  }
  .perf-hotspot-table th,
  .perf-hotspot-table td {
    padding: 0.3rem 0.5rem;
    border-bottom: 1px solid rgba(148, 163, 184, 0.1);
    text-align: left;
  }
  .perf-hotspot-table th {
    color: var(--text-soft, #94a3b8);
    font-weight: 500;
  }
  .perf-hotspot-table .num { text-align: right; }
  .perf-fn-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    color: var(--text, #e2e8f0);
    white-space: nowrap;
    max-width: 18rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .perf-fn-page {
    color: var(--text-soft, #94a3b8);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--fs-lg);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 14rem;
  }
  .perf-fn-cc {
    color: var(--c-action);
    font-weight: 700;
  }
  .perf-fn-line {
    color: var(--text-soft, #94a3b8);
  }

  /* Reduced-motion: suppress animations (there are none here — all
     static SVGs — but guard for future additions). */
  @media (prefers-reduced-motion: reduce) {
    .perf-chart-svg path { transition: none !important; }
  }
</style>
