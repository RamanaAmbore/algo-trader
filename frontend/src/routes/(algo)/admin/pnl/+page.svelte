<script>
  import { onMount } from 'svelte';
  import { aggCompact, pctFmt } from '$lib/format.js';

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

  // CSV upload
  let csvAccount    = $state('');
  let csvDate       = $state('');
  /** @type {File|null} */
  let csvFile       = $state(null);
  let csvLoading    = $state(false);
  let csvError      = $state('');
  let csvResult     = $state(/** @type {any} */ (null));
  let dragging      = $state(false);

  // ── Helpers ────────────────────────────────────────────────────────────────
  function todayIST() {
    // Use Indian locale to get today's date regardless of client tz.
    return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
  }

  function pnlClass(v) {
    if (v == null || isNaN(v)) return '';
    return v >= 0 ? 'pos' : 'neg';
  }

  /** @param {number|null} v */
  function fmt(v) { return v == null ? '—' : aggCompact(v); }

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

  onMount(() => {
    const today = todayIST();
    fromDate = today;
    toDate   = today;
    csvDate  = today;
    load();
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
      // Reload the range after a successful upload
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

  // ── Derived data (filtered by drilldown account) ──────────────────────────
  const visibleSymbols = $derived.by(() => {
    if (!data?.by_symbol) return [];
    if (!filterAccount) return data.by_symbol;
    // No account field on by_symbol — show all when filtering
    return data.by_symbol;
  });

  const visibleDaily = $derived.by(() => {
    if (!data?.daily_series) return [];
    return data.daily_series;
  });

  // Daily chart — very simple SVG sparkline
  const sparkPath = $derived.by(() => {
    const pts = visibleDaily;
    if (!pts || pts.length < 2) return '';
    const vals = pts.map(p => p.total_pnl ?? 0);
    const minV = Math.min(...vals);
    const maxV = Math.max(...vals);
    const span = maxV - minV || 1;
    const W = 480, H = 60, PAD = 4;
    const xs = pts.map((_, i) => PAD + (i / (pts.length - 1)) * (W - PAD * 2));
    const ys = vals.map(v => H - PAD - ((v - minV) / span) * (H - PAD * 2));
    return xs.map((x, i) => `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${ys[i].toFixed(1)}`).join(' ');
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
  <button class="sim-btn sim-btn-order" onclick={load} disabled={loading}>
    {loading ? 'Loading…' : 'Apply'}
  </button>
  {#if filterAccount}
    <button class="sim-btn sim-btn-order" style="background:rgba(251,191,36,0.08)"
            onclick={() => filterAccount = null}>
      Clear filter: {filterAccount}
    </button>
  {/if}
</div>

{#if error}
  <div class="err-banner">{error}</div>
{/if}

{#if data}
  <!-- ── Summary card ────────────────────────────────────────────── -->
  <div class="summary-card">
    <div class="kv">
      <span class="kv-lbl">Total P&L</span>
      <span class="kv-val {pnlClass(data.summary.total_pnl)}">{fmt(data.summary.total_pnl)}</span>
    </div>
    <div class="kv">
      <span class="kv-lbl">Day P&L</span>
      <span class="kv-val {pnlClass(data.summary.day_pnl)}">{fmt(data.summary.day_pnl)}</span>
    </div>
    <div class="kv">
      <span class="kv-lbl">Dates</span>
      <span class="kv-val">{data.summary.n_dates}</span>
    </div>
    <div class="kv">
      <span class="kv-lbl">Accounts</span>
      <span class="kv-val">{data.summary.n_accounts}</span>
    </div>
    <div class="kv">
      <span class="kv-lbl">Range</span>
      <span class="kv-val">{data.from_date} → {data.to_date}</span>
    </div>
  </div>

  <!-- ── By segment ──────────────────────────────────────────────── -->
  <section class="section">
    <h2 class="section-heading">By segment</h2>
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
  </section>

  <!-- ── By account ──────────────────────────────────────────────── -->
  <section class="section">
    <h2 class="section-heading">By account</h2>
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
  </section>

  <!-- ── Top 50 symbols ──────────────────────────────────────────── -->
  <section class="section">
    <h2 class="section-heading">Top symbols <span class="section-sub">(by |total P&L|, max 50)</span></h2>
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
  </section>

  <!-- ── Daily series ────────────────────────────────────────────── -->
  <section class="section">
    <h2 class="section-heading">Daily series</h2>
    {#if visibleDaily.length === 0}
      <p class="empty-hint">No daily data in range.</p>
    {:else}
      {#if visibleDaily.length >= 2}
        <!-- Sparkline chart -->
        <div class="spark-wrap">
          <svg viewBox="0 0 480 60" class="spark-svg" aria-hidden="true">
            <!-- zero baseline -->
            <line x1="4" y1="30" x2="476" y2="30"
                  stroke="rgba(200,216,240,0.08)" stroke-width="1" />
            <path d={sparkPath}
                  fill="none"
                  stroke={data.summary.total_pnl >= 0 ? '#4ade80' : '#f87171'}
                  stroke-width="1.5" />
          </svg>
        </div>
      {/if}
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
  </section>
{/if}

<!-- ── CSV upload card ─────────────────────────────────────────────── -->
<section class="section upload-card">
  <h2 class="section-heading">Upload Kite P&L CSV <span class="section-sub">(backfill)</span></h2>
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
    <button class="sim-btn sim-btn-order" onclick={uploadCsv} disabled={csvLoading}>
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
</section>

<style>
  /* ── Page title ────────────────────────────────────────────────── */
  .page-title-chip { font-size: 0.9rem; font-weight: 700; color: #fbbf24; margin: 0; }
  .title-sub { font-weight: 400; color: #7e97b8; font-size: 0.75rem; }

  /* ── Filter bar ────────────────────────────────────────────────── */
  .filter-bar {
    display: flex;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.7rem;
  }
  .fb-label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.62rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
  }
  .fb-date  { width: 8.5rem; font-size: 0.7rem; padding: 0.22rem 0.4rem; }
  .fb-text  { width: 8rem;   font-size: 0.7rem; padding: 0.22rem 0.4rem; }
  .fb-select { width: 9rem;  font-size: 0.7rem; padding: 0.22rem 0.4rem; }

  /* ── Error banner ──────────────────────────────────────────────── */
  .err-banner {
    background: rgba(239,68,68,0.12);
    border: 1px solid rgba(239,68,68,0.4);
    border-radius: 4px;
    color: #fca5a5;
    font-size: 0.68rem;
    padding: 0.35rem 0.65rem;
    margin-bottom: 0.5rem;
    font-family: ui-monospace, monospace;
  }

  /* ── Summary card ──────────────────────────────────────────────── */
  .summary-card {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem 1.2rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px;
    padding: 0.65rem 0.9rem;
    margin-bottom: 0.75rem;
  }
  .kv { display: flex; flex-direction: column; gap: 0.1rem; min-width: 7rem; }
  .kv-lbl {
    font-size: 0.58rem;
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

  /* ── Sections ──────────────────────────────────────────────────── */
  .section { margin-bottom: 1rem; }
  .section-sub { font-size: 0.65rem; font-weight: 400; color: #7e97b8; }

  .empty-hint {
    font-size: 0.7rem;
    color: #4e6080;
    font-family: ui-monospace, monospace;
    padding: 0.3rem 0;
  }

  /* ── Table ─────────────────────────────────────────────────────── */
  .tbl-wrap { overflow-x: auto; }
  .pnl-tbl {
    width: 100%;
    border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
  }
  .pnl-tbl th {
    text-align: left;
    padding: 0.28rem 0.55rem;
    color: #7e97b8;
    font-weight: 600;
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 1px solid rgba(255,255,255,0.07);
    white-space: nowrap;
  }
  .pnl-tbl td {
    padding: 0.25rem 0.55rem;
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

  .clickable { cursor: pointer; }
  .row-active td { background: rgba(251,191,36,0.08) !important; }

  .seg-pill {
    display: inline-block;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-size: 0.58rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    background: rgba(125,211,252,0.12);
    color: #7dd3fc;
  }

  /* ── Sparkline ─────────────────────────────────────────────────── */
  .spark-wrap {
    background: rgba(0,0,0,0.2);
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 4px;
    padding: 0.3rem 0.4rem;
    margin-bottom: 0.4rem;
    overflow: hidden;
  }
  .spark-svg { display: block; width: 100%; height: 60px; }

  /* ── Upload card ───────────────────────────────────────────────── */
  .upload-card {
    background: linear-gradient(180deg, #1b2840 0%, #141e33 100%);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
    padding: 0.75rem 0.9rem;
  }
  .upload-hint {
    font-size: 0.65rem;
    color: #7e97b8;
    margin: 0 0 0.6rem;
    font-family: ui-monospace, monospace;
  }
  .upload-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.55rem;
  }
  .upload-actions { margin-top: 0.55rem; }

  /* Drop zone */
  .drop-zone {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    border: 1.5px dashed rgba(125,211,252,0.25);
    border-radius: 5px;
    padding: 0.75rem 1rem;
    cursor: pointer;
    transition: border-color 0.12s, background 0.12s;
    font-size: 0.68rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    position: relative;
  }
  .drop-zone:hover  { border-color: rgba(125,211,252,0.5); background: rgba(125,211,252,0.04); }
  .drag-over { border-color: #7dd3fc !important; background: rgba(125,211,252,0.08) !important; }
  .has-file  { border-color: rgba(74,222,128,0.4); color: #c8d8f0; }
  .drop-prompt { color: #4e6080; }
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
    font-size: 0.68rem;
    font-family: ui-monospace, monospace;
    color: #4ade80;
    background: rgba(74,222,128,0.07);
    border: 1px solid rgba(74,222,128,0.2);
    border-radius: 4px;
    padding: 0.3rem 0.65rem;
  }
</style>
