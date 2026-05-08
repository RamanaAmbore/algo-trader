<script>
  import { onMount } from 'svelte';
  import { aggCompact, pctFmt } from '$lib/format.js';
  import PnlPanel from '$lib/PnlPanel.svelte';
  import { fetchPnlBenchmarks } from '$lib/api.js';

  // ── State ──────────────────────────────────────────────────────────────────
  /** @type {string} */
  let fromDate   = $state('');
  /** @type {string} */
  let toDate     = $state('');
  let segment    = $state('all');
  let kind       = $state('all');

  /** @type {any} */
  let data       = $state(null);
  let loading    = $state(false);
  let error      = $state('');

  // Drilldown — click a by_account row to filter the other tables.
  /** @type {string|null} */
  let filterAccount = $state(null);

  // By-agent section — collapsed by default
  let agentExpanded = $state(false);

  // CSV upload — collapsed by default
  let csvExpanded   = $state(false);
  let csvAccount    = $state('');
  let csvDate       = $state('');
  /** @type {File|null} */
  let csvFile       = $state(null);
  let csvLoading    = $state(false);
  let csvError      = $state('');
  let csvResult     = $state(/** @type {any} */ (null));
  let dragging      = $state(false);

  // ── Benchmark chart state ────────────────────────────────────────────────
  const BENCHMARKS = [
    { id: 'NIFTY 50',           color: '#7dd3fc', label: 'NIFTY 50'           },
    { id: 'BANK NIFTY',         color: '#c084fc', label: 'BANK NIFTY'         },
    { id: 'SENSEX',             color: '#14b8a6', label: 'SENSEX'             },
    { id: 'NIFTY MIDCAP 100',   color: '#f43f5e', label: 'MIDCAP 100'         },
    { id: 'NIFTY SMALLCAP 100', color: '#4ade80', label: 'SMALLCAP 100'       },
  ];

  // Portfolio series — always shown when pct_change_from_start data exists
  const PORTFOLIO_COLOR = '#fbbf24';

  /** @returns {{ symbol: string, name: string, closes: Array<{date:string, pct_change_from_start:number}> }} */
  const portfolioSeries = $derived.by(() => {
    const ds = /** @type {any[]} */ (data?.daily_series ?? []);
    return {
      symbol: 'PORTFOLIO',
      name: 'Portfolio',
      closes: ds
        .filter(r => r.pct_change_from_start != null)
        .map(r => ({ date: r.date, pct_change_from_start: r.pct_change_from_start })),
    };
  });

  const portfolioActive = $derived(portfolioSeries.closes.length > 0);
  /** @type {Set<string>} */
  let bmActive = $state(new Set(['NIFTY 50']));
  /** @type {any[] | null} */
  let bmSeries  = $state(null);
  let bmLoading = $state(false);
  let bmError   = $state('');

  // SVG chart interactions
  let hovX      = $state(/** @type {number|null} */ (null));

  // ── Helpers ────────────────────────────────────────────────────────────────
  function todayIST() {
    return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  }

  function pnlClass(v) {
    if (v == null || isNaN(v)) return '';
    return v >= 0 ? 'pos' : 'neg';
  }

  /** @param {number|null} v */
  function fmt(v) { return v == null ? '—' : aggCompact(v); }

  /** @param {number} v */
  function fmtPct(v) { return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; }

  // ── Data fetching ──────────────────────────────────────────────────────────
  async function load() {
    loading = true;
    error   = '';
    try {
      const token = sessionStorage.getItem('ramboq_token');
      const p = new URLSearchParams({ segment, kind });
      if (fromDate) p.set('from_date', fromDate);
      if (toDate)   p.set('to_date',   toDate);
      const res = await fetch(`/api/admin/pnl/range?${p}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || `HTTP ${res.status}`);
      }
      data = await res.json();
    } catch (e) {
      error = /** @type {any} */ (e)?.message ?? 'Load failed.';
      data  = null;
    } finally {
      loading = false;
    }
  }

  async function loadBenchmarks() {
    if (!fromDate || !toDate) return;
    bmLoading = true;
    bmError   = '';
    try {
      const syms = [...bmActive].join(',');
      const res = await fetchPnlBenchmarks({ from_date: fromDate, to_date: toDate, symbols: syms || 'NIFTY 50' });
      bmSeries = res?.series ?? [];
    } catch (e) {
      bmError  = /** @type {any} */ (e)?.message ?? 'Benchmarks unavailable.';
      bmSeries = null;
    } finally {
      bmLoading = false;
    }
  }

  function toggleBenchmark(/** @type {string} */ id) {
    const next = new Set(bmActive);
    if (next.has(id)) { next.delete(id); } else { next.add(id); }
    bmActive = next;
    loadBenchmarks();
  }

  onMount(() => {
    const today = todayIST();
    fromDate = today;
    toDate   = today;
    csvDate  = today;
    load();
    loadBenchmarks();
  });

  // ── CSV upload ─────────────────────────────────────────────────────────────
  async function uploadCsv() {
    if (!csvAccount.trim()) { csvError = 'Account is required.'; return; }
    if (!csvFile)            { csvError = 'Select a CSV file.';  return; }
    csvLoading = true;
    csvError   = '';
    csvResult  = null;
    try {
      const token = sessionStorage.getItem('ramboq_token');
      const fd = new FormData();
      fd.append('account', csvAccount.trim());
      fd.append('date',    csvDate || todayIST());
      fd.append('file',    csvFile);
      const res = await fetch('/api/admin/pnl/upload-csv', {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || `HTTP ${res.status}`);
      }
      csvResult = await res.json();
      await load();
    } catch (e) {
      csvError = /** @type {any} */ (e)?.message ?? 'Upload failed.';
    } finally {
      csvLoading = false;
    }
  }

  function onFileDrop(e) {
    e.preventDefault();
    dragging = false;
    const f = e.dataTransfer?.files?.[0];
    if (f && f.name.endsWith('.csv')) csvFile = f;
  }

  // ── Derived data ───────────────────────────────────────────────────────────
  const visibleSymbols = $derived.by(() => {
    if (!data?.by_symbol) return [];
    return data.by_symbol;
  });

  const visibleDaily = $derived.by(() => {
    if (!data?.daily_series) return [];
    return data.daily_series;
  });

  // ── Benchmark SVG chart ────────────────────────────────────────────────────
  // Chart dimensions
  const W = 560, H = 160, PAD_L = 42, PAD_R = 12, PAD_T = 12, PAD_B = 24;
  const CW = W - PAD_L - PAD_R;
  const CH = H - PAD_T - PAD_B;

  // Merge all visible series (benchmarks + portfolio) into a unified set of
  // dates + pct values.  Portfolio is always included when data exists.
  const chartData = $derived.by(() => {
    const bmVisible = (bmSeries ?? []).filter(
      /** @param {any} s */ s => bmActive.has(s.symbol) && s.closes?.length > 0
    );
    const portVisible = portfolioActive ? [portfolioSeries] : [];
    const allSeries = [...bmVisible, ...portVisible];

    if (allSeries.length === 0) return null;

    // Combine all dates (sorted)
    const dateSet = new Set(allSeries.flatMap(s => s.closes.map(c => c.date)));
    const dates = [...dateSet].sort();
    if (dates.length === 0) return null;

    // Build per-series lookup
    const lookup = new Map(allSeries.map(s => [
      s.symbol,
      new Map(s.closes.map(c => [c.date, c.pct_change_from_start])),
    ]));

    // Y domain from all pct values across benchmarks + portfolio
    const allPct = allSeries.flatMap(s => s.closes.map(c => c.pct_change_from_start));
    const yMin = Math.min(0, ...allPct);
    const yMax = Math.max(0, ...allPct);
    const ySpan = yMax - yMin || 1;

    return { visible: bmVisible, portVisible, allSeries, dates, lookup, yMin, yMax, ySpan };
  });

  function xOf(/** @type {number} */ i, /** @type {number} */ total) {
    return PAD_L + (total <= 1 ? CW / 2 : (i / (total - 1)) * CW);
  }

  function yOf(/** @type {number} */ pct) {
    if (!chartData) return PAD_T;
    return PAD_T + CH - ((pct - chartData.yMin) / chartData.ySpan) * CH;
  }

  function buildLinePath(/** @type {string} */ symbol) {
    if (!chartData) return '';
    const map = chartData.lookup.get(symbol);
    if (!map) return '';
    let d = '';
    chartData.dates.forEach((date, i) => {
      const v = map.get(date);
      if (v == null) return;
      const x = xOf(i, chartData.dates.length).toFixed(1);
      const y = yOf(v).toFixed(1);
      d += d ? ` L${x} ${y}` : `M${x} ${y}`;
    });
    return d;
  }

  // Y-axis grid labels
  const yGridLines = $derived.by(() => {
    if (!chartData) return [];
    const { yMin, yMax, ySpan } = chartData;
    const step = ySpan / 4;
    return Array.from({ length: 5 }, (_, i) => {
      const pct = yMin + step * i;
      return { pct, y: yOf(pct) };
    });
  });

  // X-axis labels — start, mid, end
  const xLabels = $derived.by(() => {
    if (!chartData || chartData.dates.length === 0) return [];
    const { dates } = chartData;
    const n = dates.length;
    const idxs = n === 1 ? [0] : [0, Math.floor(n / 2), n - 1];
    return idxs.map(i => ({ label: dates[i].slice(5), x: xOf(i, n) }));
  });

  // Hover vertical line + tooltip
  function handleMouseMove(/** @type {MouseEvent} */ e) {
    const svg = /** @type {SVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const px = e.clientX - rect.left;
    // Map pixel to chart-space x
    hovX = (px / rect.width) * W;
  }
  function handleMouseLeave() { hovX = null; }

  const hovIdx = $derived.by(() => {
    if (hovX == null || !chartData) return null;
    const { dates } = chartData;
    if (dates.length === 0) return null;
    const frac = Math.max(0, Math.min(1, (hovX - PAD_L) / CW));
    return Math.round(frac * (dates.length - 1));
  });

  const hovDate = $derived(hovIdx != null && chartData ? chartData.dates[hovIdx] : null);

  const hovValues = $derived.by(() => {
    if (hovIdx == null || !chartData || hovDate == null) return [];
    return chartData.allSeries.map(s => ({
      symbol: s.symbol,
      color: s.symbol === 'PORTFOLIO' ? PORTFOLIO_COLOR : (BENCHMARKS.find(b => b.id === s.symbol)?.color ?? '#c8d8f0'),
      label: s.symbol === 'PORTFOLIO' ? 'Portfolio' : (BENCHMARKS.find(b => b.id === s.symbol)?.label ?? s.symbol),
      pct: chartData.lookup.get(s.symbol)?.get(hovDate) ?? null,
    }));
  });

  const hovLineX = $derived(hovIdx != null && chartData
    ? xOf(hovIdx, chartData.dates.length)
    : null);

  // Latest % for each visible series (legend chips) — portfolio always first
  const legendValues = $derived.by(() => {
    if (!chartData) return [];
    const bmChips = chartData.visible.map(s => {
      const closes = s.closes;
      const last = closes.length > 0 ? closes[closes.length - 1].pct_change_from_start : null;
      const color = BENCHMARKS.find(b => b.id === s.symbol)?.color ?? '#c8d8f0';
      const label = BENCHMARKS.find(b => b.id === s.symbol)?.label ?? s.symbol;
      return { symbol: s.symbol, label, color, pct: last, isPortfolio: false };
    });
    if (!portfolioActive) return bmChips;
    const portCloses = portfolioSeries.closes;
    const portLast = portCloses.length > 0 ? portCloses[portCloses.length - 1].pct_change_from_start : null;
    return [
      { symbol: 'PORTFOLIO', label: 'Portfolio', color: PORTFOLIO_COLOR, pct: portLast, isPortfolio: true },
      ...bmChips,
    ];
  });
</script>

<svelte:head>
  <title>P&L Range · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">P&L <span class="title-sub">· date range</span></h1>
</div>

<!-- ── Filter bar ─────────────────────────────────────────────────── -->
<div class="filter-bar">
  <label class="fb-label">
    From
    <input type="date" class="field-input fb-date" bind:value={fromDate} />
  </label>
  <label class="fb-label">
    To
    <input type="date" class="field-input fb-date" bind:value={toDate} />
  </label>
  <label class="fb-label">
    Segment
    <select class="field-input fb-select" bind:value={segment}>
      <option value="all">All</option>
      <option value="equity">Equity</option>
      <option value="derivatives">Derivatives</option>
      <option value="commodity">Commodity</option>
      <option value="currency">Currency</option>
    </select>
  </label>
  <label class="fb-label">
    Kind
    <select class="field-input fb-select" bind:value={kind}>
      <option value="all">All</option>
      <option value="holdings">Holdings</option>
      <option value="positions">Positions</option>
    </select>
  </label>
  <button class="algo-btn" onclick={() => { load(); loadBenchmarks(); }} disabled={loading}>
    {loading ? 'Loading…' : 'Apply'}
  </button>
  {#if filterAccount}
    <button class="algo-btn algo-btn-dim" onclick={() => filterAccount = null}>
      Clear: {filterAccount}
    </button>
  {/if}
</div>

{#if error}
  <div class="err-banner">{error}</div>
{/if}

{#if data}
  <!-- ── Summary card — 5 KVs in a single row ──────────────────── -->
  <div class="card summary-row">
    <div class="kv">
      <span class="kv-lbl">Total P&L</span>
      <span class="kv-val {pnlClass(data.summary.total_pnl)}">{fmt(data.summary.total_pnl)}</span>
    </div>
    <div class="kv-div"></div>
    <div class="kv">
      <span class="kv-lbl">Day P&L</span>
      <span class="kv-val {pnlClass(data.summary.day_pnl)}">{fmt(data.summary.day_pnl)}</span>
    </div>
    <div class="kv-div"></div>
    <div class="kv">
      <span class="kv-lbl">Dates</span>
      <span class="kv-val">{data.summary.n_dates}</span>
    </div>
    <div class="kv-div"></div>
    <div class="kv">
      <span class="kv-lbl">Accounts</span>
      <span class="kv-val">{data.summary.n_accounts}</span>
    </div>
    <div class="kv-div"></div>
    <div class="kv">
      <span class="kv-lbl">Range</span>
      <span class="kv-val range-val">{data.from_date} → {data.to_date}</span>
    </div>
  </div>

  <!-- ── Performance chart ─────────────────────────────────────── -->
  <div class="card chart-card">
    <div class="card-head-row">
      <span class="section-head">Performance — % from {fromDate}</span>
      <div class="bm-toggles">
        {#each BENCHMARKS as bm}
          <button
            class="bm-chip {bmActive.has(bm.id) ? 'bm-on' : 'bm-off'}"
            style="--bm-color:{bm.color}"
            onclick={() => toggleBenchmark(bm.id)}
          >{bm.label}</button>
        {/each}
      </div>
    </div>

    {#if !portfolioActive && data && !data.start_capital}
      <div class="no-capital-hint">
        Portfolio % requires capital data on the from-date — pick a date with snapshot rows.
      </div>
    {/if}

    {#if bmError}
      <div class="err-banner" style="margin-top:0.3rem">{bmError}</div>
    {:else if bmLoading}
      <div class="chart-placeholder">Loading benchmarks…</div>
    {:else if !chartData}
      <div class="chart-placeholder">No benchmark data — toggle a series above or widen the date range.</div>
    {:else}
      <!-- SVG chart -->
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <svg
        class="perf-svg"
        viewBox="0 0 {W} {H}"
        aria-label="Performance chart"
        role="img"
        onmousemove={handleMouseMove}
        onmouseleave={handleMouseLeave}
      >
        <!-- grid -->
        {#each yGridLines as { pct, y }}
          <line x1={PAD_L} y1={y.toFixed(1)} x2={W - PAD_R} y2={y.toFixed(1)}
                stroke="rgba(200,216,240,0.09)" stroke-width="1" />
          <text x={PAD_L - 4} y={(y + 3.5).toFixed(1)}
                font-size="9" fill="#7e97b8" text-anchor="end"
                font-family="ui-monospace,monospace">{fmtPct(pct)}</text>
        {/each}
        <!-- zero line -->
        {#if chartData.yMin < 0 && chartData.yMax > 0}
          <line x1={PAD_L} y1={yOf(0).toFixed(1)} x2={W - PAD_R} y2={yOf(0).toFixed(1)}
                stroke="rgba(200,216,240,0.22)" stroke-width="1" />
        {/if}
        <!-- x-axis labels -->
        {#each xLabels as { label, x }}
          <text x={x.toFixed(1)} y={H - 4} font-size="9" fill="#7e97b8"
                text-anchor="middle" font-family="ui-monospace,monospace">{label}</text>
        {/each}
        <!-- benchmark series lines -->
        {#each chartData.visible as s}
          {@const color = BENCHMARKS.find(b => b.id === s.symbol)?.color ?? '#c8d8f0'}
          <path d={buildLinePath(s.symbol)}
                fill="none"
                stroke={color}
                stroke-width="1.8"
                stroke-linejoin="round"
                stroke-linecap="round" />
        {/each}
        <!-- portfolio line — amber, 2px, always on top of benchmarks -->
        {#if portfolioActive}
          <path d={buildLinePath('PORTFOLIO')}
                fill="none"
                stroke={PORTFOLIO_COLOR}
                stroke-width="2"
                stroke-linejoin="round"
                stroke-linecap="round" />
        {/if}
        <!-- hover crosshair -->
        {#if hovLineX != null && hovDate}
          <line x1={hovLineX.toFixed(1)} y1={PAD_T}
                x2={hovLineX.toFixed(1)} y2={H - PAD_B}
                stroke="rgba(200,216,240,0.35)" stroke-width="1" />
        {/if}
      </svg>

      <!-- Hover tooltip -->
      {#if hovDate && hovValues.length > 0}
        <div class="hov-tip">
          <span class="hov-date">{hovDate}</span>
          {#each hovValues as v}
            <span class="hov-series" style="color:{v.color}">
              {v.label}: {v.pct != null ? fmtPct(v.pct) : '—'}
            </span>
          {/each}
        </div>
      {/if}

      <!-- Legend chips -->
      <div class="legend-row">
        {#each legendValues as lv}
          <span class="legend-chip" style="border-color:{lv.color};color:{lv.color}">
            <span class="legend-dot" style="background:{lv.color}"></span>
            {lv.label}
            {#if lv.pct != null}
              <span class="legend-pct">{fmtPct(lv.pct)}</span>
            {/if}
          </span>
        {/each}
      </div>
    {/if}
  </div>

  <!-- ── By segment ──────────────────────────────────────────────── -->
  <div class="card">
    <header class="section-head">By segment</header>
    {#if data.by_segment.length === 0}
      <p class="empty-hint">No data in range.</p>
    {:else}
      <div class="tbl-wrap">
        <table class="pnl-tbl">
          <thead>
            <tr><th>Segment</th><th>Total P&L</th><th>Day P&L</th><th>Rows</th></tr>
          </thead>
          <tbody>
            {#each data.by_segment as row}
              <tr>
                <td><span class="seg-pill">{row.segment}</span></td>
                <td class="num {pnlClass(row.total_pnl)}">{fmt(row.total_pnl)}</td>
                <td class="num {pnlClass(row.day_pnl)}">{fmt(row.day_pnl)}</td>
                <td class="num muted">{row.n_rows}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <!-- ── By account ──────────────────────────────────────────────── -->
  <div class="card">
    <header class="section-head">By account</header>
    {#if data.by_account.length === 0}
      <p class="empty-hint">No data in range.</p>
    {:else}
      <div class="tbl-wrap">
        <table class="pnl-tbl">
          <thead>
            <tr><th>Account</th><th>Segment</th><th>Kind</th><th>Total P&L</th><th>Day P&L</th><th>Rows</th></tr>
          </thead>
          <tbody>
            {#each data.by_account as row}
              <tr
                class="clickable {filterAccount === row.account ? 'row-active' : ''}"
                onclick={() => filterAccount = filterAccount === row.account ? null : row.account}
                title="Click to filter by {row.account}"
              >
                <td class="mono">{row.account}</td>
                <td><span class="seg-pill">{row.segment}</span></td>
                <td class="muted">{row.kind}</td>
                <td class="num {pnlClass(row.total_pnl)}">{fmt(row.total_pnl)}</td>
                <td class="num {pnlClass(row.day_pnl)}">{fmt(row.day_pnl)}</td>
                <td class="num muted">{row.n_rows}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <!-- ── Top 50 symbols ──────────────────────────────────────────── -->
  <div class="card">
    <header class="section-head">
      Top symbols <span class="section-sub">(by |total P&L|, max 50)</span>
    </header>
    {#if visibleSymbols.length === 0}
      <p class="empty-hint">No data in range.</p>
    {:else}
      <div class="tbl-wrap">
        <table class="pnl-tbl">
          <thead>
            <tr><th>Symbol</th><th>Segment</th><th>Total P&L</th><th>Day P&L</th><th>Rows</th></tr>
          </thead>
          <tbody>
            {#each visibleSymbols as row}
              <tr>
                <td class="mono sym">{row.symbol}</td>
                <td><span class="seg-pill">{row.segment}</span></td>
                <td class="num {pnlClass(row.total_pnl)}">{fmt(row.total_pnl)}</td>
                <td class="num {pnlClass(row.day_pnl)}">{fmt(row.day_pnl)}</td>
                <td class="num muted">{row.n_rows}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <!-- ── Daily series ────────────────────────────────────────────── -->
  <div class="card">
    <header class="section-head">Daily series</header>
    {#if visibleDaily.length === 0}
      <p class="empty-hint">No daily data in range.</p>
    {:else}
      <div class="tbl-wrap">
        <table class="pnl-tbl">
          <thead>
            <tr><th>Date</th><th>Total P&L</th><th>Day P&L</th></tr>
          </thead>
          <tbody>
            {#each visibleDaily as row}
              <tr>
                <td class="mono">{row.date}</td>
                <td class="num {pnlClass(row.total_pnl)}">{fmt(row.total_pnl)}</td>
                <td class="num {pnlClass(row.day_pnl)}">{fmt(row.day_pnl)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
{/if}

<!-- ── By agent — collapsible ─────────────────────────────────────── -->
<div class="card">
  <button class="upload-toggle" onclick={() => agentExpanded = !agentExpanded}>
    <span class="section-head" style="pointer-events:none">
      By agent
    </span>
    <span class="chevron {agentExpanded ? 'open' : ''}">▸</span>
  </button>
  {#if agentExpanded}
    <div class="agent-pnl-wrap">
      <PnlPanel active={agentExpanded} />
    </div>
  {/if}
</div>

<!-- ── CSV upload card — collapsible ──────────────────────────────── -->
<div class="card upload-card">
  <button class="upload-toggle" onclick={() => csvExpanded = !csvExpanded}>
    <span class="section-head" style="pointer-events:none">
      Upload Kite P&L CSV <span class="section-sub">(backfill)</span>
    </span>
    <span class="chevron {csvExpanded ? 'open' : ''}">▸</span>
  </button>

  {#if csvExpanded}
    <p class="upload-hint">
      Export from Kite Console → Reports → P&amp;L Statement → CSV, then upload here to backfill historical data.
    </p>
    <div class="upload-row">
      <label class="fb-label">
        Account
        <input class="field-input fb-text" placeholder="e.g. ZG0790"
               bind:value={csvAccount} />
      </label>
      <label class="fb-label">
        As-of date
        <input type="date" class="field-input fb-date" bind:value={csvDate} />
      </label>
    </div>
    <!-- Drop zone -->
    <div
      class="drop-zone {dragging ? 'drag-over' : ''} {csvFile ? 'has-file' : ''}"
      role="button"
      tabindex="0"
      ondragover={(e) => { e.preventDefault(); dragging = true; }}
      ondragleave={() => dragging = false}
      ondrop={onFileDrop}
      onclick={() => document.getElementById('csv-file-input')?.click()}
      onkeydown={(e) => { if (e.key === 'Enter') document.getElementById('csv-file-input')?.click(); }}
    >
      {#if csvFile}
        <span class="drop-filename">{csvFile.name}</span>
        <button class="drop-clear" onclick={(e) => { e.stopPropagation(); csvFile = null; }}
                aria-label="Clear file">×</button>
      {:else}
        <span class="drop-prompt">Drag &amp; drop CSV or click to browse</span>
      {/if}
      <input id="csv-file-input" type="file" accept=".csv" class="hidden-input"
             onchange={(e) => { csvFile = /** @type {any} */ (e.target)?.files?.[0] ?? null; }} />
    </div>

    <div class="upload-actions">
      <button class="algo-btn" onclick={uploadCsv} disabled={csvLoading}>
        {csvLoading ? 'Uploading…' : 'Upload'}
      </button>
    </div>

    {#if csvError}
      <div class="err-banner" style="margin-top:0.4rem">{csvError}</div>
    {/if}
    {#if csvResult}
      <div class="upload-result">
        Inserted {csvResult.inserted} · Updated {csvResult.updated} · Skipped {csvResult.skipped}
      </div>
      {#if csvResult.sample?.length}
        <div class="tbl-wrap" style="margin-top:0.5rem">
          <table class="pnl-tbl">
            <thead>
              <tr><th>Symbol</th><th>Segment</th><th>Qty</th><th>Total P&L</th></tr>
            </thead>
            <tbody>
              {#each csvResult.sample as r}
                <tr>
                  <td class="mono sym">{r.symbol}</td>
                  <td><span class="seg-pill">{r.segment}</span></td>
                  <td class="num">{r.qty}</td>
                  <td class="num {pnlClass(r.total_pnl)}">{fmt(r.total_pnl)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/if}
  {/if}
</div>

<style>
  /* ── Page title ────────────────────────────────────────────────── */
  .page-title-chip { font-size: 0.9rem; font-weight: 700; color: #fbbf24; margin: 0; }
  .title-sub { font-weight: 400; color: #7e97b8; font-size: 0.75rem; }

  /* ── Filter bar ────────────────────────────────────────────────── */
  .filter-bar {
    display: flex;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-bottom: 0.6rem;
  }
  .fb-label {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
    font-size: 0.6rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .fb-date   { width: 8.5rem; font-size: 0.7rem; padding: 0.2rem 0.38rem; }
  .fb-text   { width: 8rem;   font-size: 0.7rem; padding: 0.2rem 0.38rem; }
  .fb-select { width: 9rem;   font-size: 0.7rem; padding: 0.2rem 0.38rem; }

  /* ── Error banner ──────────────────────────────────────────────── */
  .err-banner {
    background: rgba(239,68,68,0.12);
    border: 1px solid rgba(239,68,68,0.4);
    border-radius: 4px;
    color: #fca5a5;
    font-size: 0.65rem;
    padding: 0.3rem 0.6rem;
    margin-bottom: 0.45rem;
    font-family: ui-monospace, monospace;
  }

  /* ── Shared card ───────────────────────────────────────────────── */
  .card {
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-radius: 5px;
    padding: 0.5rem 0.75rem;
    margin-bottom: 0.55rem;
  }

  /* ── Section header ────────────────────────────────────────────── */
  .section-head {
    display: block;
    font-size: 0.55rem;
    color: #fbbf24;
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    margin-bottom: 0.45rem;
  }
  .section-sub {
    font-size: 0.55rem;
    font-weight: 400;
    color: #7e97b8;
    text-transform: none;
    letter-spacing: 0;
  }

  /* ── Summary row — 5 KVs horizontal ───────────────────────────── */
  .summary-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.3rem 0;
    padding: 0.45rem 0.75rem;
  }
  .kv {
    display: flex;
    flex-direction: column;
    gap: 0.08rem;
    padding: 0 0.75rem;
    min-width: 6rem;
  }
  .kv-div {
    width: 1px;
    height: 1.8rem;
    background: rgba(255,255,255,0.1);
    flex-shrink: 0;
  }
  .kv-lbl {
    font-size: 0.55rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .kv-val {
    font-size: 0.82rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    color: #c8d8f0;
    font-variant-numeric: tabular-nums;
  }
  .kv-val.pos { color: #4ade80; }
  .kv-val.neg { color: #f87171; }
  .range-val  { font-size: 0.68rem; font-weight: 400; }

  /* ── Chart card ────────────────────────────────────────────────── */
  .chart-card { padding: 0.5rem 0.75rem 0.55rem; }
  .card-head-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-bottom: 0.35rem;
  }
  .card-head-row .section-head { margin-bottom: 0; }
  .bm-toggles {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-left: auto;
  }
  .bm-chip {
    font-size: 0.58rem;
    font-family: ui-monospace, monospace;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.12rem 0.45rem;
    border-radius: 3px;
    cursor: pointer;
    transition: opacity 0.12s, background 0.12s;
    border: 1px solid var(--bm-color, #c8d8f0);
  }
  .bm-chip.bm-on {
    background: color-mix(in srgb, var(--bm-color) 18%, transparent);
    color: var(--bm-color);
  }
  .bm-chip.bm-off {
    background: transparent;
    color: rgba(200,216,240,0.35);
    border-color: rgba(200,216,240,0.18);
  }

  .perf-svg {
    display: block;
    width: 100%;
    height: auto;
    max-height: 160px;
    overflow: visible;
  }

  .chart-placeholder {
    font-size: 0.65rem;
    color: #4e6080;
    font-family: ui-monospace, monospace;
    padding: 1.5rem 0;
    text-align: center;
  }

  .no-capital-hint {
    font-size: 0.6rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    background: rgba(126,151,184,0.06);
    border: 1px solid rgba(126,151,184,0.18);
    border-radius: 3px;
    padding: 0.22rem 0.55rem;
    margin-bottom: 0.35rem;
  }

  /* Hover tooltip */
  .hov-tip {
    display: flex;
    flex-wrap: wrap;
    gap: 0.2rem 0.75rem;
    align-items: center;
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    padding: 0.2rem 0;
  }
  .hov-date {
    color: #c8d8f0;
    font-weight: 600;
  }
  .hov-series {
    font-variant-numeric: tabular-nums;
  }

  /* Legend */
  .legend-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-top: 0.35rem;
  }
  .legend-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.58rem;
    font-family: ui-monospace, monospace;
    font-weight: 600;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    border: 1px solid currentColor;
    opacity: 0.85;
  }
  .legend-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .legend-pct {
    font-variant-numeric: tabular-nums;
    opacity: 0.9;
  }

  /* ── Table ─────────────────────────────────────────────────────── */
  .tbl-wrap { overflow-x: auto; }
  .pnl-tbl {
    width: 100%;
    border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
  }
  .pnl-tbl th {
    text-align: left;
    padding: 0.22rem 0.45rem;
    color: #7e97b8;
    font-weight: 600;
    font-size: 0.58rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    white-space: nowrap;
  }
  .pnl-tbl td {
    padding: 0.22rem 0.45rem;
    color: #c8d8f0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    white-space: nowrap;
  }
  .pnl-tbl tr:hover td { background: rgba(255,255,255,0.03); }
  .pnl-tbl .num  { text-align: right; font-variant-numeric: tabular-nums; }
  .pnl-tbl .mono { font-family: ui-monospace, monospace; }
  .pnl-tbl .sym  { font-weight: 600; letter-spacing: 0.02em; }
  .pnl-tbl .muted { color: #4e6080; }
  .pnl-tbl .pos  { color: #4ade80; }
  .pnl-tbl .neg  { color: #f87171; }

  .empty-hint {
    font-size: 0.65rem;
    color: #4e6080;
    font-family: ui-monospace, monospace;
    padding: 0.25rem 0;
    margin: 0;
  }
  .clickable { cursor: pointer; }
  .row-active td { background: rgba(251,191,36,0.08) !important; }

  .seg-pill {
    display: inline-block;
    padding: 0.08rem 0.35rem;
    border-radius: 3px;
    font-size: 0.55rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    background: rgba(125,211,252,0.12);
    color: #7dd3fc;
  }

  /* ── Buttons ───────────────────────────────────────────────────── */
  .algo-btn {
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.28rem 0.75rem;
    border-radius: 4px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.1);
    color: #fbbf24;
    cursor: pointer;
    transition: background 0.12s;
  }
  .algo-btn:hover:not(:disabled) { background: rgba(251,191,36,0.18); }
  .algo-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .algo-btn-dim {
    border-color: rgba(200,216,240,0.2);
    background: rgba(200,216,240,0.05);
    color: #7e97b8;
  }

  /* ── By-agent embed ────────────────────────────────────────────── */
  .agent-pnl-wrap {
    margin-top: 0.45rem;
    /* PnlPanel ships with its own internal padding; strip the outer
       horizontal padding so its content lines up with the card edges. */
    margin-left: -0.75rem;
    margin-right: -0.75rem;
  }

  /* ── Upload card / collapsible ─────────────────────────────────── */
  .upload-toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    background: transparent;
    border: none;
    cursor: pointer;
    padding: 0;
  }
  .chevron {
    font-size: 0.75rem;
    color: #fbbf24;
    transition: transform 0.15s;
    margin-left: auto;
  }
  .chevron.open { transform: rotate(90deg); }

  .upload-hint {
    font-size: 0.62rem;
    color: #7e97b8;
    margin: 0.3rem 0 0.5rem;
    font-family: ui-monospace, monospace;
  }
  .upload-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    margin-bottom: 0.5rem;
  }
  .upload-actions { margin-top: 0.5rem; }

  /* Drop zone */
  .drop-zone {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    border: 1.5px dashed rgba(125,211,252,0.25);
    border-radius: 5px;
    padding: 0.65rem 1rem;
    cursor: pointer;
    transition: border-color 0.12s, background 0.12s;
    font-size: 0.65rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    position: relative;
  }
  .drop-zone:hover  { border-color: rgba(125,211,252,0.5); background: rgba(125,211,252,0.04); }
  .drag-over { border-color: #7dd3fc !important; background: rgba(125,211,252,0.08) !important; }
  .has-file  { border-color: rgba(74,222,128,0.4); color: #c8d8f0; }
  .drop-prompt  { color: #4e6080; }
  .drop-filename { font-weight: 600; color: #c8d8f0; }
  .drop-clear {
    background: transparent;
    border: none;
    color: #f87171;
    font-size: 1rem;
    cursor: pointer;
    line-height: 1;
    padding: 0 0.2rem;
  }
  .hidden-input {
    position: absolute;
    inset: 0;
    opacity: 0;
    width: 100%;
    height: 100%;
    cursor: pointer;
    pointer-events: none;
  }

  /* Upload result */
  .upload-result {
    margin-top: 0.4rem;
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    color: #4ade80;
    background: rgba(74,222,128,0.07);
    border: 1px solid rgba(74,222,128,0.2);
    border-radius: 4px;
    padding: 0.28rem 0.6rem;
  }
</style>
