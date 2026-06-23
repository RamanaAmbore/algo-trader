<script>
  import { onMount } from 'svelte';
  import { aggCompact, pctFmt } from '$lib/format.js';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { fetchPnlBenchmarks } from '$lib/api.js';
  import PnlPanel from '$lib/PnlPanel.svelte';
  import Select   from '$lib/Select.svelte';

  // Bindable prop — flips to `false` only after a successful fetch
  // confirms zero rows for the current filters. Defaults to `true` so
  // parents (dashboard chart card) don't auto-collapse during the
  // initial loading window before /pnl-benchmarks resolves.
  let { hasData = $bindable(true) } = $props();

  // ── State ──────────────────────────────────────────────────────────────────
  /** @type {'today'|'5d'|'1m'|'3m'|'1y'|'ytd'|'custom'} */
  // Default 5d — the operator's natural 'show me my P&L' expectation is
  // a multi-day curve, not a single-point flat line. Today is one click
  // away on the preset strip when they want the intraday snapshot.
  let preset = $state('5d');
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

  // By-agent section — open by default; the user wants P&L attribution
  // visible at a glance, not behind a chevron.
  let agentExpanded    = $state(true);

  /** Debounce ID for auto-apply on filter change. */
  let _applyTimer = /** @type {ReturnType<typeof setTimeout> | null} */ (null);

  // Range-breakdown tabs
  /** @type {'segment'|'account'|'symbol'|'daily'} */
  let breakTab = $state('segment');

  // CSV upload modal
  let csvOpen       = $state(false);
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

  /** @param {number|null|undefined} v */
  function fmtPct(v) {
    if (v == null || !isFinite(v)) return '—';
    return pctFmt(v) + '%';
  }

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
      const next = await res.json();
      if (next) data = next;
    } catch (e) {
      // Keep the prior chart payload visible on transient failure —
      // `error` banner above the chart signals the staleness. Previously
      // `data = null` wiped the chart on every poll failure.
      error = /** @type {any} */ (e)?.message ?? 'Load failed.';
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

  /** Resolve a preset key to {from, to} ISO date strings. */
  function presetRange(/** @type {typeof preset} */ p) {
    const today = todayIST();
    if (p === 'today') return { from: today, to: today };
    if (p === 'ytd')   return { from: today.slice(0, 4) + '-01-01', to: today };
    const t = new Date(today + 'T00:00:00');
    if (p === '5d') t.setDate(t.getDate() - 5);
    if (p === '1m') t.setMonth(t.getMonth() - 1);
    if (p === '3m') t.setMonth(t.getMonth() - 3);
    if (p === '1y') t.setFullYear(t.getFullYear() - 1);
    return { from: t.toISOString().slice(0, 10), to: today };
  }

  /** Apply a range preset. 'custom' leaves dates alone so the operator
   *  can pick anything in the From / To inputs. */
  function applyPreset(/** @type {typeof preset} */ p) {
    preset = p;
    if (p !== 'custom') {
      const r = presetRange(p);
      fromDate = r.from;
      toDate   = r.to;
    }
    scheduleApply(0);
  }

  /** Auto-apply on filter mutation — debounced so rapid typing in date
   *  inputs collapses to a single fetch. */
  function scheduleApply(/** @type {number} */ ms = 300) {
    if (_applyTimer != null) clearTimeout(_applyTimer);
    _applyTimer = setTimeout(() => {
      _applyTimer = null;
      load();
      loadBenchmarks();
    }, ms);
  }

  /** When the operator types in a date input directly, switch the preset
   *  to 'custom' so the chip strip doesn't lie about the active range. */
  function onDateChange() {
    preset = 'custom';
    scheduleApply();
  }

  let _mounted = $state(false);
  onMount(() => {
    const today = todayIST();
    // Hydrate the default range from the active preset (5d) so the first
    // fetch covers a multi-day window. The CSV upload modal still defaults
    // to today since the operator is uploading a single trading day.
    const r = presetRange(preset);
    fromDate = r.from;
    toDate   = r.to;
    csvDate  = today;
    load();
    loadBenchmarks();
    _mounted = true;
  });

  // Re-apply filters whenever the operator picks a new Segment / Kind
  // from the custom Select. (Native <select> had an onchange handler;
  // the custom component drives via $bindable, so an effect is the
  // post-mount substitute.) Guarded by _mounted so the initial state
  // doesn't double-fire alongside onMount's load() call.
  $effect(() => {
    void segment; void kind;
    if (_mounted) scheduleApply();
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

  // Latest portfolio % over the range — the last daily_series row's
  // pct_change_from_start. Null when no rows or no start_capital.
  const portfolioPct = $derived.by(() => {
    const ds = visibleDaily;
    if (!ds.length) return null;
    const last = ds[ds.length - 1];
    return last?.pct_change_from_start ?? null;
  });

  // Latest NIFTY 50 % over the range from the benchmarks payload.
  const nifty50Pct = $derived.by(() => {
    const series = (bmSeries ?? []).find(/** @param {any} s */ s => s.symbol === 'NIFTY 50');
    if (!series?.closes?.length) return null;
    return series.closes[series.closes.length - 1].pct_change_from_start;
  });

  // Alpha: portfolio % - NIFTY 50 % over the same range. Null when
  // either side is missing.
  const vsNifty = $derived.by(() => {
    if (portfolioPct == null || nifty50Pct == null) return null;
    return portfolioPct - nifty50Pct;
  });

  // Best / worst day = max/min of day_pnl across daily_series. Returns
  // {pnl, date} or null.
  const bestDay = $derived.by(() => {
    const ds = visibleDaily.filter(r => r.day_pnl != null);
    if (!ds.length) return null;
    return ds.reduce((a, b) => (a.day_pnl >= b.day_pnl ? a : b));
  });
  const worstDay = $derived.by(() => {
    const ds = visibleDaily.filter(r => r.day_pnl != null);
    if (!ds.length) return null;
    return ds.reduce((a, b) => (a.day_pnl <= b.day_pnl ? a : b));
  });

  // True when the snapshot table is empty for this range — used to
  // render the "no data" banner instead of empty cards.
  const hasNoData = $derived(data != null && data.summary?.n_dates === 0);

  // Mirror the empty/has-data state up to the optional `hasData` prop
  // so parents (dashboard chart card) can auto-collapse when both the
  // Intraday curve and PnlAnalysis are empty. Stays `true` while
  // loading (data == null) to avoid premature collapse.
  $effect(() => {
    hasData = data == null ? true : (data.summary?.n_dates ?? 0) > 0;
  });

  // ── Benchmark SVG chart ────────────────────────────────────────────────────
  const W = 560, H = 160, PAD_L = 42, PAD_R = 12, PAD_T = 12, PAD_B = 24;
  const CW = W - PAD_L - PAD_R;
  const CH = H - PAD_T - PAD_B;

  const chartData = $derived.by(() => {
    const bmVisible = (bmSeries ?? []).filter(
      /** @param {any} s */ s => bmActive.has(s.symbol) && s.closes?.length > 0
    );
    const portVisible = portfolioActive ? [portfolioSeries] : [];
    const allSeries = [...bmVisible, ...portVisible];

    if (allSeries.length === 0) return null;

    const dateSet = new Set(allSeries.flatMap(s => s.closes.map(c => c.date)));
    const dates = [...dateSet].sort();
    if (dates.length === 0) return null;

    const lookup = new Map(allSeries.map(s => [
      s.symbol,
      new Map(s.closes.map(c => [c.date, c.pct_change_from_start])),
    ]));

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

  const yGridLines = $derived.by(() => {
    if (!chartData) return [];
    const { yMin, yMax, ySpan } = chartData;
    const step = ySpan / 4;
    return Array.from({ length: 5 }, (_, i) => {
      const pct = yMin + step * i;
      return { pct, y: yOf(pct) };
    });
  });

  const xLabels = $derived.by(() => {
    if (!chartData || chartData.dates.length === 0) return [];
    const { dates } = chartData;
    const n = dates.length;
    const idxs = n === 1 ? [0] : [0, Math.floor(n / 2), n - 1];
    return idxs.map(i => ({ label: dates[i].slice(5), x: xOf(i, n) }));
  });

  function handleMouseMove(/** @type {MouseEvent} */ e) {
    const svg = /** @type {SVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const px = e.clientX - rect.left;
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

<!-- ── Range presets + filters ───────────────────────────────────── -->
<div class="filter-bar">
  <div class="preset-strip" role="tablist" aria-label="Range preset">
    {#each [['today','Today'], ['5d','5D'], ['1m','1M'], ['3m','3M'], ['1y','1Y'], ['ytd','YTD'], ['custom','Custom']] as [val, label]}
      <button class="preset-chip {preset === val ? 'preset-on' : ''}"
              role="tab"
              aria-selected={preset === val}
              onclick={() => applyPreset(/** @type {any} */ (val))}>
        {label}
      </button>
    {/each}
  </div>
  {#if preset === 'custom'}
    <input type="date" class="field-input fb-date" title="From date"
           bind:value={fromDate} onchange={onDateChange} />
    <span class="fb-sep" aria-hidden="true">→</span>
    <input type="date" class="field-input fb-date" title="To date"
           bind:value={toDate} onchange={onDateChange} />
  {/if}
  <div class="fb-select" title="Segment">
    <Select ariaLabel="Segment" bind:value={segment}
      options={[
        { value: 'all',         label: 'All Segments' },
        { value: 'equity',      label: 'Equity' },
        { value: 'derivatives', label: 'Derivatives' },
        { value: 'commodity',   label: 'Commodity' },
        { value: 'currency',    label: 'Currency' },
      ]} />
  </div>
  <div class="fb-select" title="Kind">
    <Select ariaLabel="Kind" bind:value={kind}
      options={[
        { value: 'all',       label: 'All Kinds' },
        { value: 'holdings',  label: 'Holdings' },
        { value: 'positions', label: 'Positions' },
      ]} />
  </div>
  {#if loading}<span class="filter-spinner">Loading…</span>{/if}
  <button class="algo-btn algo-btn-dim csv-btn"
          title="Backfill historical P&L from a Kite Console CSV export"
          onclick={() => csvOpen = true}>
    ↑ Backfill CSV
  </button>
</div>

{#if error}
  <div class="err-banner">{error}</div>
{/if}

{#if data && hasNoData}
  <div class="no-data-banner">
    <strong>No P&L snapshots in this date range.</strong>
    Snapshots run automatically at 15:35 IST every trading day. Try a wider
    range, or use <button type="button" class="link-btn" onclick={() => csvOpen = true}>↑ Backfill CSV</button>
    to import historical Kite Console data.
  </div>
{/if}

{#if data}
  <!-- Headline KVs — only metrics that aren't already in the global
       PositionStrip. T / D / DP are redundant with PositionStrip's
       P&L / H∆+P∆ / P∆ on the Today preset; on multi-day ranges they'd
       sum but the breakdown tabs below already show the raw rows. -->
  <div class="card summary-row">
    <div class="kv" title="Portfolio % (return over range)">
      <span class="kv-lbl">P%</span>
      <span class="kv-val {pnlClass(portfolioPct)}">{fmtPct(portfolioPct)}</span>
    </div>
    <div class="kv" title="vs NIFTY 50 (alpha over range)">
      <span class="kv-lbl">vN</span>
      <span class="kv-val {pnlClass(vsNifty)}">{fmtPct(vsNifty)}</span>
    </div>
    <div class="kv" title={bestDay ? `Best day · ${bestDay.date}` : 'Best day'}>
      <span class="kv-lbl">B↑</span>
      {#if bestDay}
        <span class="kv-val {pnlClass(bestDay.day_pnl)}">{fmt(bestDay.day_pnl)}</span>
      {:else}
        <span class="kv-val muted">—</span>
      {/if}
    </div>
    <div class="kv" title={worstDay ? `Worst day · ${worstDay.date}` : 'Worst day'}>
      <span class="kv-lbl">W↓</span>
      {#if worstDay}
        <span class="kv-val {pnlClass(worstDay.day_pnl)}">{fmt(worstDay.day_pnl)}</span>
      {:else}
        <span class="kv-val muted">—</span>
      {/if}
    </div>
    <div class="summary-meta">
      {data.summary.n_dates}d · {data.summary.n_accounts}a · {data.from_date} → {data.to_date}
    </div>
  </div>

  <!-- Performance chart -->
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
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <svg
        class="perf-svg"
        viewBox="0 0 {W} {H}"
        aria-label="Performance chart"
        role="img"
        onmousemove={handleMouseMove}
        onmouseleave={handleMouseLeave}
      >
        {#each yGridLines as { pct, y }}
          <line x1={PAD_L} y1={y.toFixed(1)} x2={W - PAD_R} y2={y.toFixed(1)}
                stroke="rgba(200,216,240,0.18)" stroke-width="1"
                stroke-dasharray="2 3" />
          <text x={PAD_L - 4} y={(y + 3.5).toFixed(1)}
                font-size="11" fill="#c8d8f0" font-weight="600" text-anchor="end"
                font-family="ui-monospace,monospace">{fmtPct(pct)}</text>
        {/each}
        {#if chartData.yMin < 0 && chartData.yMax > 0}
          <line x1={PAD_L} y1={yOf(0).toFixed(1)} x2={W - PAD_R} y2={yOf(0).toFixed(1)}
                stroke="rgba(200,216,240,0.45)" stroke-width="1" />
        {/if}
        {#each xLabels as { x }}
          <line x1={x.toFixed(1)} y1={PAD_T} x2={x.toFixed(1)} y2={(H - PAD_B).toFixed(1)}
                stroke="rgba(200,216,240,0.10)" stroke-width="1" stroke-dasharray="2 3" />
          <!-- X-axis date text removed per dashboard preference; hover
               tooltip still surfaces exact date on demand. -->
        {/each}
        {#each chartData.visible as s}
          {@const color = BENCHMARKS.find(b => b.id === s.symbol)?.color ?? '#c8d8f0'}
          <path d={buildLinePath(s.symbol)}
                fill="none"
                stroke={color}
                stroke-width="1.8"
                stroke-linejoin="round"
                stroke-linecap="round" />
        {/each}
        {#if portfolioActive}
          <path d={buildLinePath('PORTFOLIO')}
                fill="none"
                stroke={PORTFOLIO_COLOR}
                stroke-width="2"
                stroke-linejoin="round"
                stroke-linecap="round" />
        {/if}
        {#if hovLineX != null && hovDate}
          <line x1={hovLineX.toFixed(1)} y1={PAD_T}
                x2={hovLineX.toFixed(1)} y2={H - PAD_B}
                stroke="rgba(200,216,240,0.35)" stroke-width="1" />
        {/if}
      </svg>

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

  <!-- Range breakdown — tabbed -->
  <div class="card">
    <div class="tab-strip" role="tablist" aria-label="Range breakdown">
      <button class="tab {breakTab === 'segment' ? 'tab-on' : ''}"
              role="tab" aria-selected={breakTab === 'segment'}
              onclick={() => breakTab = 'segment'}>Segment</button>
      <button class="tab {breakTab === 'account' ? 'tab-on' : ''}"
              role="tab" aria-selected={breakTab === 'account'}
              onclick={() => breakTab = 'account'}>Account</button>
      <button class="tab {breakTab === 'symbol' ? 'tab-on' : ''}"
              role="tab" aria-selected={breakTab === 'symbol'}
              onclick={() => breakTab = 'symbol'}>Symbol</button>
      <button class="tab {breakTab === 'daily' ? 'tab-on' : ''}"
              role="tab" aria-selected={breakTab === 'daily'}
              onclick={() => breakTab = 'daily'}>Daily</button>
    </div>

    {#if breakTab === 'segment'}
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
    {:else if breakTab === 'account'}
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
                <tr>
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
    {:else if breakTab === 'symbol'}
      {#if visibleSymbols.length === 0}
        <p class="empty-hint">No data in range.</p>
      {:else}
        <p class="tab-hint">Top by |total P&L|, max 50.</p>
        <div class="tbl-wrap">
          <table class="pnl-tbl">
            <thead>
              <tr><th>Symbol</th><th>Segment</th><th>Total P&L</th><th>Day P&L</th><th>Rows</th></tr>
            </thead>
            <tbody>
              {#each visibleSymbols as row}
                <tr>
                  <td class="mono sym">{formatSymbol(row.symbol)}</td>
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
    {:else if breakTab === 'daily'}
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
    {/if}
  </div>
{/if}

<!-- By agent — collapsible (default closed) -->
<div class="card">
  <button class="upload-toggle" onclick={() => agentExpanded = !agentExpanded}>
    <span class="section-head" style="pointer-events:none">
      By agent <span class="section-sub">— P&L attribution</span>
    </span>
    <span class="chevron {agentExpanded ? 'open' : ''}">▸</span>
  </button>
  {#if agentExpanded}
    <div style="margin-top:0.75rem">
      <PnlPanel active={agentExpanded} />
    </div>
  {/if}
</div>

<!-- CSV upload modal -->
{#if csvOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-backdrop" onclick={() => csvOpen = false}></div>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="csv-modal-title">
    <div class="modal-head">
      <h2 id="csv-modal-title" class="modal-title">Backfill Kite P&L CSV</h2>
      <button class="modal-x" aria-label="Close" onclick={() => csvOpen = false}>×</button>
    </div>
    <p class="upload-hint">
      Export from Kite Console → Reports → P&amp;L Statement → CSV, then upload here.
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
      <button class="algo-btn algo-btn-dim" onclick={() => csvOpen = false}>Close</button>
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
                  <td class="mono sym">{formatSymbol(r.symbol)}</td>
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
  </div>
{/if}

<style>
  .filter-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-bottom: 0.6rem;
  }
  .preset-strip {
    display: inline-flex;
    gap: 0;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 4px;
    overflow: hidden;
  }
  .preset-chip {
    background: transparent;
    border: none;
    padding: 0.22rem 0.55rem;
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
    cursor: pointer;
    border-right: 1px solid var(--algo-amber-border-soft);
    transition: background 0.1s, color 0.1s;
  }
  .preset-chip:last-child { border-right: none; }
  .preset-chip:hover { color: var(--algo-slate); background: var(--algo-amber-bg-soft); }
  .preset-chip.preset-on {
    background: var(--algo-amber-bg);
    color: #fbbf24;
  }
  .filter-spinner {
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    color: var(--algo-muted);
    padding: 0 0.3rem;
  }
  .no-data-banner {
    background: rgba(126,151,184,0.06);
    border: 1px solid rgba(126,151,184,0.25);
    border-radius: 4px;
    color: var(--algo-slate);
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    padding: 0.45rem 0.7rem;
    margin-bottom: 0.55rem;
    line-height: 1.5;
  }
  .no-data-banner strong { color: #fbbf24; font-weight: 700; }
  .link-btn {
    background: transparent;
    border: none;
    color: #fbbf24;
    cursor: pointer;
    font-family: inherit;
    font-size: inherit;
    padding: 0;
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .link-btn:hover { color: #fcd34d; }
  .muted { color: #4e6080 !important; }
  .fb-label {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
    font-size: 0.6rem;
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .fb-date   { width: 8rem;   font-size: 0.7rem; padding: 0.2rem 0.38rem; }
  .fb-text   { width: 8rem;   font-size: 0.7rem; padding: 0.2rem 0.38rem; }
  .fb-select { width: 9rem;   font-size: 0.7rem; padding: 0.2rem 0.38rem; }
  .fb-sep {
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    padding: 0 0.05rem;
  }
  .csv-btn   { margin-left: auto; }

  .err-banner {
    background: rgba(248,113,113,0.12);
    border: 1px solid rgba(248,113,113,0.4);
    border-radius: 4px;
    color: #f87171;
    font-size: 0.65rem;
    padding: 0.3rem 0.6rem;
    margin-bottom: 0.45rem;
    font-family: ui-monospace, monospace;
  }

  .card {
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: 5px;
    padding: 0.5rem 0.75rem;
    margin-bottom: 0.55rem;
  }

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
    color: var(--algo-muted);
    text-transform: none;
    letter-spacing: 0;
  }

  .summary-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0;
    padding: 0.4rem 0.55rem;
  }
  .kv {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.05rem;
    padding: 0 0.45rem;
    min-width: 0;
    border-right: 1px solid rgba(255,255,255,0.08);
    cursor: help;
  }
  /* The 7th .kv sits before .summary-meta — drop its right border so
     it doesn't paint a stray separator next to the meta tail.
     :last-of-type would target .summary-meta (last <div> of any type)
     instead of the last .kv element, hence :has(+ .summary-meta). */
  .kv:has(+ .summary-meta) { border-right: none; }
  .kv-lbl {
    font-size: 0.55rem;
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .kv-val {
    font-size: 0.78rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .kv-val.pos { color: #4ade80; }
  .kv-val.neg { color: #f87171; }
  .summary-meta {
    margin-left: auto;
    font-size: 0.55rem;
    font-family: ui-monospace, monospace;
    color: var(--algo-muted);
    padding-left: 0.5rem;
    white-space: nowrap;
  }
  /* Mobile: drop padding, allow wrap of the meta line only. KVs stay
     on one row down to ~340px viewport. */
  @media (max-width: 640px) {
    .summary-row { padding: 0.35rem 0.4rem; }
    .kv { padding: 0 0.32rem; }
    .kv-val { font-size: 0.7rem; }
    .summary-meta { width: 100%; margin-left: 0; padding-left: 0; padding-top: 0.2rem; }
  }

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
    border: 1px solid var(--bm-color, var(--algo-slate));
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
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    background: rgba(126,151,184,0.06);
    border: 1px solid rgba(126,151,184,0.18);
    border-radius: 3px;
    padding: 0.22rem 0.55rem;
    margin-bottom: 0.35rem;
  }

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
    color: var(--algo-slate);
    font-weight: 600;
  }
  .hov-series {
    font-variant-numeric: tabular-nums;
  }

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

  .tab-strip {
    display: flex;
    gap: 0.05rem;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    margin-bottom: 0.45rem;
  }
  .tab {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    padding: 0.28rem 0.7rem;
    font-size: 0.6rem;
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    cursor: pointer;
    transition: color 0.12s, border-color 0.12s;
  }
  .tab:hover { color: var(--algo-slate); }
  .tab.tab-on {
    color: #fbbf24;
    border-bottom-color: #fbbf24;
  }
  .tab-hint {
    font-size: 0.6rem;
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    margin: 0 0 0.35rem;
  }

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
    color: var(--algo-muted);
    font-weight: 600;
    font-size: 0.58rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    white-space: nowrap;
  }
  .pnl-tbl td {
    padding: 0.22rem 0.45rem;
    color: var(--algo-slate);
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
  .algo-btn:hover:not(:disabled) { background: var(--algo-amber-bg); }
  .algo-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .algo-btn-dim {
    border-color: rgba(200,216,240,0.2);
    background: rgba(200,216,240,0.05);
    color: var(--algo-muted);
  }

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

  .modal-backdrop {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.55);
    z-index: 49;
  }
  .modal {
    position: fixed;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 6px;
    padding: 0.85rem 1rem;
    width: min(28rem, 92vw);
    max-height: 90vh;
    overflow-y: auto;
    z-index: 50;
    box-shadow: 0 8px 28px rgba(0,0,0,0.55);
  }
  .modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }
  .modal-title {
    font-size: 0.75rem;
    color: #fbbf24;
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    margin: 0;
  }
  .modal-x {
    background: transparent;
    border: none;
    color: var(--algo-muted);
    font-size: 1.1rem;
    cursor: pointer;
    line-height: 1;
    padding: 0 0.2rem;
  }
  .modal-x:hover { color: #fbbf24; }

  .upload-hint {
    font-size: 0.62rem;
    color: var(--algo-muted);
    margin: 0.3rem 0 0.5rem;
    font-family: ui-monospace, monospace;
  }
  .upload-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    margin-bottom: 0.5rem;
  }
  .upload-actions {
    margin-top: 0.5rem;
    display: flex;
    gap: 0.4rem;
  }

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
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    position: relative;
  }
  .drop-zone:hover  { border-color: rgba(125,211,252,0.5); background: rgba(125,211,252,0.04); }
  .drag-over { border-color: #7dd3fc !important; background: rgba(125,211,252,0.08) !important; }
  .has-file  { border-color: rgba(74,222,128,0.4); color: var(--algo-slate); }
  .drop-prompt  { color: #4e6080; }
  .drop-filename { font-weight: 600; color: var(--algo-slate); }
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
