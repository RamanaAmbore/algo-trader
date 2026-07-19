<!--
  /admin/metrics — code-health snapshot history + per-metric trends.

  Backed by `GET /api/admin/code-metrics/*` (admin-guarded). Rows are
  produced out-of-band by `scripts/capture_metrics.py` either manually
  by the operator or from the deploy pipeline. This page is READ-ONLY
  — every cell here came from a capture run.

  Layout:
    Top:    snapshot table (release, captured_at, key headline metrics)
    Middle: trend chart row — small SVG line chart per metric
    Bottom: drill-in modal for the raw_payload (forensics)
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { nowStamp, lastRefreshAt, formatDualTz, marketAwareInterval } from '$lib/stores';
  import {
    fetchCodeMetricsList,
    fetchCodeMetricsDetail,
    fetchCodeMetricsTrend,
  } from '$lib/api';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { userRole } from '$lib/rbac';
  import InfoHint from '$lib/InfoHint.svelte';
  import { METRIC_META } from '$lib/data/metricMetadata.js';

  /** Core metrics the operator wants charted side-by-side. Each entry
   * maps a CodeMetricsSnapshot column (or virtual test sub-key) to a
   * small SVG line chart config (label + unit suffix + direction). */
  const TREND_TILES = /** @type {const} */ ([
    { key: 'backend_complexity_avg',          label: 'Backend complexity (avg)',     unit: '',  good: 'lower' },
    { key: 'backend_complexity_max',          label: 'Backend complexity (max)',     unit: '',  good: 'lower' },
    { key: 'backend_loc',                     label: 'Backend LOC',                  unit: '',  good: 'lower' },
    { key: 'backend_stale_count',             label: 'Backend stale-code count',     unit: '',  good: 'lower' },
    { key: 'backend_coverage_pct',            label: 'Backend coverage',             unit: '%', good: 'higher' },
    { key: 'frontend_loc',                    label: 'Frontend LOC',                 unit: '',  good: 'lower' },
    { key: 'frontend_complexity_avg',         label: 'Frontend complexity (avg)',    unit: '',  good: 'lower' },
    { key: 'frontend_duplicated_lines',       label: 'Frontend duplicated lines',    unit: '',  good: 'lower' },
    { key: 'frontend_stale_count',            label: 'Frontend stale-code count',    unit: '',  good: 'lower' },
    { key: 'bug_count_since_last_release',    label: 'Bug fixes since prev tag',     unit: '',  good: 'lower' },
    // Test response-time virtual keys (extracted from test_response_times JSONB).
    // Populated after deploy pipeline starts passing --with-test-times.
    { key: 'test_backend_max_s',              label: 'Slowest test — backend (s)',   unit: 's', good: 'lower' },
    { key: 'test_backend_total_wall_time_s',  label: 'Backend test wall time (s)',   unit: 's', good: 'lower' },
  ]);

  /** @type {Array<any>} */
  let rows = $state([]);
  let total = $state(0);
  let _showLiveTs = $state(false);
  let loading = $state(false);
  let error = $state('');

  /** @type {Record<string, Array<{release_tag:string,captured_at:string,value:number|null}>>} */
  let trendData = $state({});
  let trendLoading = $state(false);

  /** Selected row for the drill-in raw_payload modal. */
  let selected = $state(/** @type {any} */ (null));
  let selectedPayload = $state(/** @type {any} */ (null));

  async function load() {
    loading = true; error = '';
    try {
      const r = await fetchCodeMetricsList(50, 0);
      rows = Array.isArray(r?.rows) ? r.rows : [];
      total = Number(r?.total ?? 0);
    } catch (e) {
      error = e?.message || 'Code-metrics fetch failed';
      toast.error(`Metrics load failed: ${e?.message || 'unknown error'}`);
    } finally {
      loading = false;
    }
  }

  async function loadTrends() {
    trendLoading = true;
    try {
      const results = await Promise.allSettled(
        TREND_TILES.map(tile => fetchCodeMetricsTrend(tile.key, 50))
      );
      /** @type {Record<string, Array<{release_tag:string,captured_at:string,value:number|null}>>} */
      const next = {};
      for (let i = 0; i < TREND_TILES.length; i++) {
        const r = results[i];
        next[TREND_TILES[i].key] = r.status === 'fulfilled' && Array.isArray(r.value?.points)
          ? r.value.points
          : [];
      }
      trendData = next;
    } finally {
      trendLoading = false;
    }
  }

  async function openDrill(/** @type {any} */ row) {
    selected = row;
    selectedPayload = null;
    try {
      const d = await fetchCodeMetricsDetail(row.release_tag);
      selectedPayload = d?.raw_payload || {};
    } catch (e) {
      toast.error(`Detail fetch failed: ${e?.message || 'unknown'}`);
      selectedPayload = { _error: String(e?.message || e) };
    }
  }
  function closeDrill() {
    selected = null;
    selectedPayload = null;
  }

  function fmtTs(/** @type {string} */ iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      const ist = d.toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
        hour12: false,
      });
      return ist + ' IST';
    } catch { return iso; }
  }

  function fmtNum(/** @type {number|null|undefined} */ n) {
    if (n === null || n === undefined) return '—';
    if (typeof n !== 'number') return String(n);
    if (Number.isInteger(n)) return n.toLocaleString('en-IN');
    return n.toFixed(2);
  }

  /** Build SVG path for a metric's trend series. Returns {path, area, dots, yMin, yMax}.
   * Values that are null are skipped (and break the path), so a single
   * gap doesn't drop the whole line to 0. */
  function trendPath(/** @type {Array<{value:number|null}>} */ points, w = 200, h = 60) {
    if (!points || points.length === 0) {
      return { path: '', area: '', dots: [], yMin: 0, yMax: 1 };
    }
    const vals = points.map(p => (p.value === null || p.value === undefined ? null : Number(p.value)));
    const nonNull = vals.filter(v => v !== null);
    if (nonNull.length === 0) {
      return { path: '', area: '', dots: [], yMin: 0, yMax: 1 };
    }
    const yMin = Math.min(...nonNull);
    const yMax = Math.max(...nonNull);
    const span = yMax === yMin ? 1 : yMax - yMin;
    const n = points.length;
    const xStep = n === 1 ? 0 : w / (n - 1);

    let path = '';
    let area = '';
    let lastValid = false;
    const dots = [];

    for (let i = 0; i < n; i++) {
      const v = vals[i];
      if (v === null) { lastValid = false; continue; }
      const x = i * xStep;
      const y = h - ((v - yMin) / span) * h * 0.9 - h * 0.05; // 5% padding
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
    // Close the area path to baseline.
    if (dots.length > 0) {
      area += ` L${dots[dots.length - 1].x.toFixed(1)},${h}Z`;
    }
    return { path, area, dots, yMin, yMax };
  }

  /** Latest non-null value for the headline-card display. */
  function latestValue(/** @type {Array<{value:number|null}>} */ points) {
    if (!points) return null;
    for (let i = points.length - 1; i >= 0; i--) {
      if (points[i].value !== null && points[i].value !== undefined) {
        return points[i].value;
      }
    }
    return null;
  }

  onMount(() => {
    Promise.all([load(), loadTrends()]);
  });
</script>

<svelte:head>
  <title>Code Metrics · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Code Metrics</h1>
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
    <RefreshButton onClick={() => Promise.all([load(), loadTrends()])} loading={loading} label="code metrics" />
    <PageHeaderActions />
  </span>
</div>

{#if error}
  <EmptyState title="Could not load code metrics" hint={error} icon="warn" />
{:else if loading && rows.length === 0}
  <LoadingSkeleton variant="card" rows={3} />
{:else if rows.length === 0}
  <EmptyState
    title="No snapshots yet"
    icon="info"
  >
    {#snippet hintBody()}
      Run <code>python scripts/capture_metrics.py --release-tag &lt;tag&gt;</code>
      from the project root to capture the first snapshot. The deploy
      pipeline can also call it automatically — see
      <code>webhook/deploy.sh</code>.
    {/snippet}
  </EmptyState>
{:else}

  <!-- Trend tiles — 2-col grid on desktop, stacked on mobile. -->
  <div class="metrics-trends">
    {#each TREND_TILES as tile}
      {@const pts = trendData[tile.key] || []}
      {@const drawn = trendPath(pts)}
      {@const latest = latestValue(pts)}
      <div class="metrics-tile">
        <div class="metrics-tile-head">
          <span class="metrics-tile-label">
            {tile.label}
            {#if METRIC_META[tile.key]}
              <InfoHint content={METRIC_META[tile.key]} popup={true} maxWidth="26rem" />
            {/if}
          </span>
          <span class="metrics-tile-latest">
            {fmtNum(latest)}{tile.unit}
          </span>
        </div>
        <svg viewBox="0 0 200 60" preserveAspectRatio="none" class="metrics-tile-svg" aria-hidden="true">
          {#if drawn.area}
            <path d={drawn.area} class="metrics-area" />
          {/if}
          {#if drawn.path}
            <path d={drawn.path} class="metrics-line" />
          {/if}
        </svg>
        <div class="metrics-tile-meta">
          <span>{pts.length} pts</span>
          <span class="metrics-tile-range">
            {#if drawn.yMin !== undefined && drawn.yMax !== undefined && drawn.yMin !== drawn.yMax}
              {fmtNum(drawn.yMin)} → {fmtNum(drawn.yMax)}
            {/if}
          </span>
        </div>
      </div>
    {/each}
  </div>

  <h2 class="metrics-h2">Snapshots ({total})</h2>

  <div class="metrics-table-wrap">
    <table class="metrics-table">
      <thead>
        <tr>
          <th>Release</th>
          <th>Captured</th>
          <th class="num"><span class="metric-label">BE LOC<InfoHint content={METRIC_META.backend_loc} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">BE cx avg<InfoHint content={METRIC_META.backend_complexity_avg} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">BE cx max<InfoHint content={METRIC_META.backend_complexity_max} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">BE stale<InfoHint content={METRIC_META.backend_stale_count} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">BE cov %<InfoHint content={METRIC_META.backend_coverage_pct} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">FE LOC<InfoHint content={METRIC_META.frontend_loc} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">FE cx avg<InfoHint content={METRIC_META.frontend_complexity_avg} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">FE dup<InfoHint content={METRIC_META.frontend_duplicated_lines} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">FE stale<InfoHint content={METRIC_META.frontend_stale_count} popup={true} maxWidth="26rem" /></span></th>
          <th class="num"><span class="metric-label">Bugs<InfoHint content={METRIC_META.bug_count_since_last_release} popup={true} maxWidth="26rem" /></span></th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each rows as r}
          <tr>
            <td class="metrics-tag">
              <code>{r.release_tag}</code>
              {#if r.git_sha}
                <span class="metrics-sha" title={r.git_sha}>{r.git_sha.slice(0, 7)}</span>
              {/if}
            </td>
            <td class="metrics-ts">{fmtTs(r.captured_at)}</td>
            <td class="num">{fmtNum(r.backend_loc)}</td>
            <td class="num">{fmtNum(r.backend_complexity_avg)}</td>
            <td class="num">{fmtNum(r.backend_complexity_max)}</td>
            <td class="num">{fmtNum(r.backend_stale_count)}</td>
            <td class="num">{fmtNum(r.backend_coverage_pct)}</td>
            <td class="num">{fmtNum(r.frontend_loc)}</td>
            <td class="num">{fmtNum(r.frontend_complexity_avg)}</td>
            <td class="num">{fmtNum(r.frontend_duplicated_lines)}</td>
            <td class="num">{fmtNum(r.frontend_stale_count)}</td>
            <td class="num">{fmtNum(r.bug_count_since_last_release)}</td>
            <td><button class="metrics-drill" onclick={() => openDrill(r)}>Detail</button></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

{/if}

{#if selected}
  <!-- Drill-in modal — raw_payload + per-page latency JSON. -->
  <div class="metrics-modal-overlay" onclick={closeDrill} role="presentation">
    <div
      class="metrics-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="metrics-modal-title"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => { if (e.key === 'Escape') closeDrill(); }}
      tabindex="-1"
    >
      <div class="metrics-modal-head">
        <h3 id="metrics-modal-title">
          <code>{selected.release_tag}</code>
          <span class="metrics-modal-ts">{fmtTs(selected.captured_at)}</span>
        </h3>
        <button class="metrics-modal-close" onclick={closeDrill}>×</button>
      </div>
      <div class="metrics-modal-body">
        {#if selected.notes}
          <div class="metrics-modal-notes">{selected.notes}</div>
        {/if}

        {#if selected.test_response_times}
          {@const trt = selected.test_response_times}
          <h4>Test response times</h4>
          <div class="metrics-test-grid">
            {#each [['backend', 'Backend (pytest)'], ['frontend', 'Frontend (Playwright)']] as [side, label]}
              {@const d = trt[side]}
              {#if d && typeof d === 'object' && !d._skipped && !d._error}
                <div class="metrics-test-side">
                  <div class="metrics-test-side-label">{label}</div>
                  <div class="metrics-test-kv">
                    <span>Total tests</span><span class="num">{d.total_tests ?? '—'}</span>
                    <span>Wall time</span><span class="num">{d.total_wall_time_s != null ? d.total_wall_time_s.toFixed(2) + 's' : '—'}</span>
                    <span>Median</span><span class="num">{d.median_s != null ? d.median_s.toFixed(3) + 's' : '—'}</span>
                    <span>Slowest</span><span class="num">{d.max_s != null ? d.max_s.toFixed(3) + 's' : '—'}</span>
                    <span>Slow (&gt;{d.slow_threshold_s ?? 1}s)</span><span class="num">{d.slow_count ?? '—'}</span>
                  </div>
                  {#if d.top_10_slowest && d.top_10_slowest.length > 0}
                    <div class="metrics-test-slow-label">Top slowest tests</div>
                    <ol class="metrics-test-slow-list">
                      {#each d.top_10_slowest as t, i}
                        <li>
                          <span class="metrics-test-dur">{t.duration_s.toFixed(3)}s</span>
                          <span class="metrics-test-name">{t.name}</span>
                        </li>
                      {/each}
                    </ol>
                  {/if}
                </div>
              {:else}
                <div class="metrics-test-side">
                  <div class="metrics-test-side-label">{label}</div>
                  <div class="metrics-test-skip">{d?._skipped || d?._error || 'Not captured'}</div>
                </div>
              {/if}
            {/each}
          </div>
        {:else}
          <h4>Test response times</h4>
          <div class="metrics-test-skip">Not yet captured — run capture script with <code>--with-test-times</code>.</div>
        {/if}

        <h4>Per-page latency</h4>
        <pre class="metrics-json">{JSON.stringify(selected.per_page_latency_ms || {}, null, 2)}</pre>
        <h4>Raw payload (tool outputs)</h4>
        {#if selectedPayload === null}
          <LoadingSkeleton variant="card" rows={2} />
        {:else}
          <pre class="metrics-json metrics-json-raw">{JSON.stringify(selectedPayload, null, 2)}</pre>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  .metrics-trends {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 0.6rem;
    margin: 0.8rem 0 1.2rem;
  }
  .metrics-tile {
    background: rgba(34, 211, 238, 0.06);
    border: 1px solid rgba(34, 211, 238, 0.25);
    border-radius: 6px;
    padding: 0.5rem 0.6rem;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .metrics-tile-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.4rem;
  }
  .metrics-tile-label {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--fs-lg);
    color: var(--text-soft, #94a3b8);
    line-height: 1.2;
  }
  .metrics-tile-latest {
    font-weight: 600;
    color: var(--text, #e2e8f0);
    font-variant-numeric: tabular-nums;
  }
  .metrics-tile-svg {
    width: 100%;
    height: 60px;
    display: block;
  }
  .metrics-area {
    fill: rgba(34, 211, 238, 0.18);
    stroke: none;
  }
  .metrics-line {
    fill: none;
    stroke: var(--c-info);
    stroke-width: 1.4;
  }
  .metrics-tile-meta {
    display: flex;
    justify-content: space-between;
    font-size: var(--fs-md);
    color: var(--text-soft, #94a3b8);
    font-variant-numeric: tabular-nums;
  }
  /* Canonical .algo-section-title typography — operator: "header text
     color is not consistent. GREEKS is good." Was sans-serif 1rem grey
     which read as a page sub-title instead of a card heading. */
  .metrics-h2 {
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin: 1.2rem 0 0.4rem;
    color: var(--c-action);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  .metrics-table-wrap { overflow-x: auto; }
  .metrics-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-xl);
    font-variant-numeric: tabular-nums;
  }
  .metrics-table th, .metrics-table td {
    padding: 0.32rem 0.5rem;
    border-bottom: 1px solid rgba(148, 163, 184, 0.12);
    text-align: left;
  }
  .metrics-table th { color: var(--text-soft, #94a3b8); font-weight: 500; }
  .metrics-table td.num, .metrics-table th.num { text-align: right; }
  /* Inline metric label + InfoHint chip in table headers. */
  .metric-label {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    white-space: nowrap;
  }
  .metrics-tag code { font-size: var(--fs-xl); color: #67e8f9; }
  .metrics-sha {
    margin-left: 0.4rem;
    font-size: var(--fs-lg);
    color: var(--text-soft, #94a3b8);
    font-family: var(--font-numeric);
  }
  .metrics-ts { color: var(--text-soft, #94a3b8); }
  .metrics-drill {
    background: var(--c-info-14);
    border: 1px solid rgba(34, 211, 238, 0.55);
    color: #67e8f9;
    padding: 0.18rem 0.5rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: var(--fs-lg);
  }
  .metrics-drill:hover { background: var(--c-info-22); }

  /* Modal */
  .metrics-modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }
  .metrics-modal {
    background: var(--panel-bg, #0f172a);
    border: 1px solid rgba(148, 163, 184, 0.3);
    border-radius: 6px;
    max-width: 90vw;
    max-height: 88vh;
    width: 720px;
    display: flex;
    flex-direction: column;
  }
  .metrics-modal-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.6rem 0.8rem;
    border-bottom: 1px solid rgba(148, 163, 184, 0.18);
  }
  .metrics-modal-head h3 {
    margin: 0;
    font-size: var(--fs-xl);
    display: flex;
    gap: 0.6rem;
    align-items: baseline;
  }
  .metrics-modal-ts { font-size: var(--fs-lg); color: var(--text-soft, #94a3b8); font-weight: 400; }
  .metrics-modal-close {
    background: transparent;
    border: none;
    color: var(--text-soft, #94a3b8);
    font-size: 1.4rem;
    cursor: pointer;
    line-height: 1;
    padding: 0 0.4rem;
  }
  .metrics-modal-body {
    padding: 0.6rem 0.8rem;
    overflow: auto;
  }
  .metrics-modal-body h4 {
    margin: 0.8rem 0 0.3rem;
    font-size: var(--fs-xl);
    color: var(--text-soft, #94a3b8);
    font-weight: 500;
  }
  .metrics-modal-notes {
    background: rgba(34, 211, 238, 0.06);
    border: 1px solid var(--c-info-22);
    padding: 0.4rem 0.6rem;
    border-radius: 4px;
    font-size: var(--fs-xl);
    margin-bottom: 0.4rem;
  }
  .metrics-json {
    background: rgba(0, 0, 0, 0.4);
    border: 1px solid rgba(148, 163, 184, 0.12);
    padding: 0.5rem;
    border-radius: 4px;
    font-size: var(--fs-lg);
    font-family: var(--font-numeric);
    color: var(--text-soft, #e2e8f0);
    overflow-x: auto;
    max-height: 280px;
    margin: 0;
  }
  .metrics-json-raw { max-height: 360px; }

  /* Test response times section inside the detail modal. */
  .metrics-test-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.6rem;
    margin-bottom: 0.6rem;
  }
  @media (max-width: 600px) {
    .metrics-test-grid { grid-template-columns: 1fr; }
  }
  .metrics-test-side {
    background: rgba(34, 211, 238, 0.05);
    border: 1px solid rgba(34, 211, 238, 0.18);
    border-radius: 4px;
    padding: 0.4rem 0.5rem;
  }
  .metrics-test-side-label {
    font-size: var(--fs-lg);
    font-weight: 600;
    color: var(--c-info);
    margin-bottom: 0.3rem;
  }
  .metrics-test-kv {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.1rem 0.6rem;
    font-size: var(--fs-lg);
  }
  .metrics-test-kv .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: var(--text, #e2e8f0);
  }
  .metrics-test-slow-label {
    font-size: var(--fs-lg);
    color: var(--text-soft, #94a3b8);
    margin: 0.4rem 0 0.15rem;
    font-weight: 500;
  }
  .metrics-test-slow-list {
    margin: 0;
    padding-left: 1.2rem;
    font-size: var(--fs-lg);
    color: var(--text-soft, #94a3b8);
  }
  .metrics-test-slow-list li {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    margin-bottom: 0.1rem;
    overflow: hidden;
  }
  .metrics-test-dur {
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
    color: var(--c-action);
    font-weight: 600;
    min-width: 3.5rem;
    text-align: right;
  }
  .metrics-test-name {
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .metrics-test-skip {
    font-size: var(--fs-lg);
    color: var(--text-soft, #94a3b8);
    margin: 0.2rem 0 0.4rem;
    font-style: italic;
  }
</style>
