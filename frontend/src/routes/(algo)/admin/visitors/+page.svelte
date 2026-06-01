<script>
  // Visitor log page (/admin/visitors).
  //
  // Surfaces visitor_log rows — unique IPs, request counts, geo data.
  // Role-gated: admin/designated see full data; partner sees masked IP
  // + no city/region/ASN; demo/anonymous get 403 from the backend and
  // the nav entry is hidden so they can't reach this URL in normal flow.

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, nowStamp, visibleInterval } from '$lib/stores';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import StaleBanner from '$lib/StaleBanner.svelte';
  import { fetchVisitors } from '$lib/api';

  ModuleRegistry.registerModules([AllCommunityModule]);

  // ── State ────────────────────────────────────────────────────────────
  /** @type {Array<{
   *   id: number,
   *   ip: string,
   *   seen_date: string,
   *   country: string | null,
   *   region: string | null,
   *   city: string | null,
   *   asn: string | null,
   *   request_count: number,
   *   first_seen_at: string,
   *   last_seen_at: string,
   *   last_path: string | null,
   *   user_agent: string | null,
   * }>} */
  let rows          = $state([]);
  let loading       = $state(true);
  let _initialLoad  = $state(true);   // true only for the very first fetch
  let error         = $state('');
  let days          = $state(7);
  let teardown;

  // ── ag-Grid ──────────────────────────────────────────────────────────
  let gridEl;
  /** @type {import('ag-grid-community').GridApi | null} */
  let gridApi = null;

  $effect(() => {
    if (!gridEl || gridApi) return;
    gridApi = createGrid(gridEl, /** @type {import('ag-grid-community').GridOptions} */ ({
      theme: 'legacy',
      defaultColDef: {
        resizable: true,
        sortable: true,
        suppressMovable: true,
        suppressHeaderMenuButton: true,
      },
      sortingOrder: /** @type {import('ag-grid-community').SortDirection[]} */ (['asc', 'desc', null]),
      headerHeight: 24,
      rowHeight: 24,
      columnDefs: /** @type {import('ag-grid-community').ColDef[]} */ ([
        { field: 'ip',           headerName: 'IP',
          minWidth: 100, flex: 1.4, pinned: 'left',
          cellClass: 'cell-mono', sortable: true },
        { field: 'first_seen_at', headerName: 'First',
          minWidth: 100, flex: 1.2,
          valueFormatter: ({ value }) => value ? _fmtTs(value) : '—',
          sort: 'desc' },
        { field: 'last_seen_at',  headerName: 'Last',
          minWidth: 100, flex: 1.2,
          valueFormatter: ({ value }) => value ? _fmtTs(value) : '—' },
        { field: 'request_count', headerName: 'Reqs',
          minWidth: 55, flex: 0.6,
          type: 'numericColumn',
          headerClass: 'ag-right-aligned-header',
          cellClass: 'ag-right-aligned-cell cell-amber' },
        { field: 'country',  headerName: 'Country', minWidth: 60, flex: 0.8,
          valueFormatter: ({ value }) => value ?? '—' },
        { field: 'region',   headerName: 'Region',  minWidth: 80, flex: 1,
          valueFormatter: ({ value }) => value ?? '—' },
        { field: 'city',     headerName: 'City',    minWidth: 80, flex: 1,
          valueFormatter: ({ value }) => value ?? '—' },
        { field: 'asn',      headerName: 'ASN',     minWidth: 70, flex: 0.9,
          cellClass: 'cell-mono',
          valueFormatter: ({ value }) => value ?? '—' },
        { field: 'last_path', headerName: 'Last path', minWidth: 120, flex: 2,
          valueFormatter: ({ value }) => value ?? '—' },
        { field: 'user_agent', headerName: 'UA',    minWidth: 120, flex: 2,
          valueFormatter: ({ value }) =>
            value ? (value.length > 40 ? value.slice(0, 40) + '…' : value) : '—',
          tooltipValueGetter: ({ value }) => value ?? '',
        },
      ]),
      rowData: [],
      domLayout: 'autoHeight',
      tooltipShowDelay: 200,
      overlayNoRowsTemplate:
        '<span style="font-size:0.65rem;color:#7e97b8">No visitor rows for this window.</span>',
    }));
  });

  $effect(() => {
    if (!gridApi) return;
    gridApi.setGridOption('rowData', rows);
  });

  // ── Data loading ─────────────────────────────────────────────────────
  async function load() {
    loading = true;
    try {
      rows  = await fetchVisitors(days);
      error = '';
    } catch (e) {
      error = e.message;
    } finally {
      loading      = false;
      _initialLoad = false;
    }
  }

  // Reload whenever the days filter changes. Skips the first invocation
  // because onMount triggers load() immediately.
  let _daysReady = false;
  $effect(() => {
    // read reactive dependency
    const _d = days;
    if (!_daysReady) { _daysReady = true; return; }
    load();
  });

  onMount(() => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) {
      goto('/signin');
      return;
    }
    load();
    teardown = visibleInterval(load, 60_000);
  });

  onDestroy(() => {
    teardown?.();
    gridApi?.destroy();
    gridApi = null;
  });

  // ── Derived summaries ────────────────────────────────────────────────
  const uniqueIps = $derived(rows.length);

  const totalRequests = $derived(
    rows.reduce((s, r) => s + (r.request_count ?? 0), 0)
  );

  const topCountry = $derived.by(() => {
    /** @type {Record<string, number>} */ const counts = {};
    for (const r of rows) {
      if (r.country) counts[r.country] = (counts[r.country] ?? 0) + (r.request_count ?? 0);
    }
    let best = '—', max = 0;
    for (const [k, v] of Object.entries(counts)) {
      if (v > max) { max = v; best = k; }
    }
    return best;
  });

  const topCity = $derived.by(() => {
    /** @type {Record<string, number>} */ const counts = {};
    for (const r of rows) {
      if (r.city) counts[r.city] = (counts[r.city] ?? 0) + (r.request_count ?? 0);
    }
    let best = '—', max = 0;
    for (const [k, v] of Object.entries(counts)) {
      if (v > max) { max = v; best = k; }
    }
    return best;
  });

  // ── Country bar chart data (top 8 by request count) ──────────────────
  const countryBars = $derived.by(() => {
    /** @type {Record<string, number>} */ const counts = {};
    for (const r of rows) {
      if (r.country) counts[r.country] = (counts[r.country] ?? 0) + (r.request_count ?? 0);
    }
    return Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8)
      .map(([country, count]) => ({ country, count }));
  });

  const barMax = $derived(
    countryBars.length ? Math.max(...countryBars.map(b => b.count)) : 1
  );

  // ── Helpers ──────────────────────────────────────────────────────────
  /** Format an ISO timestamp to a short readable form. */
  function _fmtTs(/** @type {string} */ iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      // Short IST-aware display: DD Mon HH:MM
      return d.toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        day: '2-digit', month: 'short',
        hour: '2-digit', minute: '2-digit',
        hour12: false,
      }).replace(',', '');
    } catch (_) { return iso; }
  }

  /** Format a whole number with en-IN grouping. */
  function _n(/** @type {number} */ v) {
    if (v == null) return '—';
    return Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }

  // Date-range toggle options
  const DATE_OPTIONS = [
    { value: 1,  label: 'Today' },
    { value: 7,  label: '7d' },
    { value: 14, label: '14d' },
    { value: 30, label: '30d' },
  ];
</script>

<svelte:head><title>Visitors | RamboQuant Analytics</title></svelte:head>

<!-- ── Page header ────────────────────────────────────────────────── -->
<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Visitors</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="visitors" />
    <PageHeaderActions />
  </span>
</div>

<!-- ── Date-range toggle ───────────────────────────────────────────── -->
<div class="range-bar">
  {#each DATE_OPTIONS as opt}
    <button
      class="range-chip {days === opt.value ? 'range-chip-active' : ''}"
      onclick={() => { days = opt.value; }}
    >{opt.label}</button>
  {/each}
</div>

<StaleBanner {error} hasData={rows.length > 0} label="Visitors" />

<!-- ── Loading skeleton (first load only) ────────────────────────── -->
{#if _initialLoad && loading}
  <div class="skeleton-wrap">
    <div class="kpi-row">
      {#each [0,1,2,3] as _}
        <div class="kpi-tile skeleton-tile"></div>
      {/each}
    </div>
    <div class="skeleton-chart"></div>
    <div class="skeleton-table">
      <div class="spinner" aria-label="Loading…"></div>
    </div>
  </div>

<!-- ── Empty state ────────────────────────────────────────────────── -->
{:else if !loading && rows.length === 0 && !error}
  <div class="vis-card empty-card">
    <div class="empty-icon">&#9737;</div>
    <p class="empty-msg">No visitors logged for this window.</p>
    <p class="empty-hint">The daily batch script runs at 03:30 IST.</p>
  </div>

<!-- ── Main content ───────────────────────────────────────────────── -->
{:else if rows.length > 0}

  <!-- KPI summary strip -->
  <div class="kpi-row">
    <div class="kpi-tile">
      <span class="kpi-val">{_n(uniqueIps)}</span>
      <span class="kpi-label">Unique IPs</span>
    </div>
    <div class="kpi-tile">
      <span class="kpi-val">{_n(totalRequests)}</span>
      <span class="kpi-label">Total requests</span>
    </div>
    <div class="kpi-tile">
      <span class="kpi-val kpi-amber">{topCountry}</span>
      <span class="kpi-label">Top country</span>
    </div>
    <div class="kpi-tile">
      <span class="kpi-val kpi-amber">{topCity}</span>
      <span class="kpi-label">Top city</span>
    </div>
  </div>

  <!-- Country bar chart (hand-rolled SVG, top 8) -->
  {#if countryBars.length > 0}
    <div class="vis-card chart-card">
      <div class="card-title">Requests by country</div>
      <svg
        class="country-chart"
        viewBox="0 0 320 {countryBars.length * 20 + 4}"
        xmlns="http://www.w3.org/2000/svg"
        aria-label="Country bar chart"
        role="img"
      >
        {#each countryBars as bar, i}
          {@const barW = Math.max(2, Math.round((bar.count / barMax) * 200))}
          {@const y = i * 20 + 2}
          <!-- country code label -->
          <text x="0" y={y + 12} class="bar-label">{bar.country}</text>
          <!-- amber bar -->
          <rect x="36" y={y + 2} width={barW} height="12" rx="2" class="bar-rect" />
          <!-- count label -->
          <text x={36 + barW + 4} y={y + 12} class="bar-count">{_n(bar.count)}</text>
        {/each}
      </svg>
    </div>
  {/if}

  <!-- Visitor table (ag-Grid ag-theme-algo) -->
  <div class="vis-card table-card">
    <div class="card-title">Visitor log</div>
    <div bind:this={gridEl} class="ag-theme-algo vis-grid"></div>
  </div>

{/if}

<style>
  /* ── Page header ──────────────────────────────────────────────────── */
  .page-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.55rem;
  }

  /* ── Date-range toggle ────────────────────────────────────────────── */
  .range-bar {
    display: flex;
    gap: 0.3rem;
    margin-bottom: 0.6rem;
    flex-wrap: wrap;
  }
  .range-chip {
    background: rgba(34, 211, 238, 0.06);
    border: 1px solid rgba(34, 211, 238, 0.25);
    border-radius: 3px;
    color: #7e97b8;
    font-size: 0.65rem;
    font-weight: 600;
    padding: 0.18rem 0.6rem;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
  }
  .range-chip:hover {
    background: rgba(34, 211, 238, 0.12);
    border-color: rgba(34, 211, 238, 0.5);
    color: #22d3ee;
  }
  .range-chip-active {
    background: rgba(34, 211, 238, 0.16);
    border-color: rgba(34, 211, 238, 0.65);
    color: #22d3ee;
  }

  /* ── Card base ────────────────────────────────────────────────────── */
  .vis-card {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251, 191, 36, 0.40);
    border-radius: 6px;
    padding: 0.5rem 0.65rem;
    margin-bottom: 0.55rem;
  }

  .card-title {
    font-size: 0.6rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    margin-bottom: 0.45rem;
    padding-bottom: 0.28rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.20);
  }

  /* ── KPI strip ────────────────────────────────────────────────────── */
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.45rem;
    margin-bottom: 0.55rem;
  }
  @media (max-width: 600px) {
    .kpi-row { grid-template-columns: repeat(2, 1fr); }
  }
  .kpi-tile {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251, 191, 36, 0.40);
    border-radius: 6px;
    padding: 0.45rem 0.6rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    min-width: 0;
  }
  .kpi-val {
    font-size: 1.4rem;
    font-weight: 700;
    color: #c8d8f0;
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .kpi-amber { color: #fbbf24; }
  .kpi-label {
    font-size: 0.6rem;
    font-weight: 500;
    color: #7e97b8;
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }

  /* ── Country bar chart ────────────────────────────────────────────── */
  .chart-card { padding: 0.5rem 0.65rem 0.55rem; }
  .country-chart {
    display: block;
    width: 100%;
    max-width: 380px;
    overflow: visible;
  }
  /* SVG text colours */
  :global(.bar-label) {
    fill: #22d3ee;
    font-family: ui-monospace, monospace;
    font-size: 9px;
    font-weight: 700;
    text-anchor: end;
    dominant-baseline: auto;
  }
  :global(.bar-rect) { fill: rgba(251, 191, 36, 0.72); }
  :global(.bar-count) {
    fill: #fbbf24;
    font-family: ui-monospace, monospace;
    font-size: 9px;
    font-weight: 600;
    dominant-baseline: auto;
  }

  /* ── Visitor table ────────────────────────────────────────────────── */
  .table-card { padding: 0.5rem 0.65rem 0.55rem; }
  .vis-grid {
    width: 100%;
    /* autoHeight manages row area; min so it doesn't collapse when empty */
    min-height: 2rem;
    font-size: 0.7rem;
  }
  /* Cell helpers for algo theme */
  :global(.ag-theme-algo .cell-mono) {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    color: #c8d8f0;
  }
  :global(.ag-theme-algo .cell-amber) {
    color: #fbbf24;
  }

  /* ── Skeleton loading ─────────────────────────────────────────────── */
  .skeleton-wrap { display: flex; flex-direction: column; gap: 0.55rem; }
  .skeleton-tile {
    background: linear-gradient(90deg,
      rgba(30, 45, 75, 0.8) 25%,
      rgba(45, 65, 100, 0.5) 50%,
      rgba(30, 45, 75, 0.8) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite linear;
    height: 4.5rem;
    border-radius: 6px;
    border: 1px solid rgba(251, 191, 36, 0.15);
  }
  .skeleton-chart {
    height: 7rem;
    border-radius: 6px;
    background: linear-gradient(90deg,
      rgba(30, 45, 75, 0.8) 25%,
      rgba(45, 65, 100, 0.5) 50%,
      rgba(30, 45, 75, 0.8) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.4s infinite linear;
    border: 1px solid rgba(251, 191, 36, 0.15);
  }
  .skeleton-table {
    height: 6rem;
    border-radius: 6px;
    background: rgba(29, 42, 68, 0.7);
    border: 1px solid rgba(251, 191, 36, 0.15);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  @keyframes shimmer {
    0%   { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
  .spinner {
    width: 1.2rem;
    height: 1.2rem;
    border: 2px solid rgba(34, 211, 238, 0.2);
    border-top-color: #22d3ee;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* ── Empty state ──────────────────────────────────────────────────── */
  .empty-card {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 2rem 1rem;
    text-align: center;
    gap: 0.4rem;
  }
  .empty-icon {
    font-size: 2rem;
    color: rgba(126, 151, 184, 0.5);
    line-height: 1;
    margin-bottom: 0.3rem;
  }
  .empty-msg {
    color: #c8d8f0;
    font-size: 0.78rem;
    font-weight: 600;
    margin: 0;
  }
  .empty-hint {
    color: #7e97b8;
    font-size: 0.65rem;
    margin: 0;
  }
</style>
