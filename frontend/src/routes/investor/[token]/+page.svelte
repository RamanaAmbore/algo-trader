<!--
  /investor/[token] — public LP-facing portal.

  Token-as-credential: anyone holding the URL reads the LP's NAV slice
  + history. No login. No registration. Operator mints from admin and
  forwards the URL. See backend/api/routes/investor.py for the gate.

  Theme is intentionally cream + champagne (the public marketing
  palette), not algo navy. LPs aren't operators; they shouldn't land
  on a trading-desk-looking page. Industry analog: Carta investor
  portal, SS&C/GP-Link LP statements — all soft / professional, not
  Bloomberg-style.
-->
<script>
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { formatDateIST } from '$lib/dateFormat.js';
  import Select from '$lib/Select.svelte';

  /** @typedef {{
   *   display_name:string, share_pct:number, contribution:number,
   *   firm_nav:number, nav_share:number, pnl:number,
   *   pnl_pct:number|null,
   *   day_delta_share:number|null, day_delta_share_pct:number|null,
   *   as_of_date:string|null
   * }} Slice */
  /** @typedef {{as_of_date:string, firm_nav:number, nav_share:number, pnl:number}} HistRow */

  /** @type {Slice|null} */
  let slice = $state(null);
  /** @type {HistRow[]} */
  let history = $state([]);
  let loading = $state(true);
  let error = $state('');

  const token = $derived(/** @type {any} */ ($page.params).token);

  async function load() {
    loading = true; error = '';
    try {
      const [sRes, hRes] = await Promise.all([
        fetch(`/api/investor/${token}/slice`),
        fetch(`/api/investor/${token}/history?days=180`),
      ]);
      if (!sRes.ok) {
        const body = await sRes.json().catch(() => ({}));
        throw new Error(body?.detail || 'This link is no longer active.');
      }
      slice = await sRes.json();
      if (hRes.ok) {
        const h = await hRes.json();
        history = Array.isArray(h?.rows) ? h.rows : [];
      }
    } catch (e) {
      error = e?.message || 'This link is no longer active.';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  // Kept local: diverges from aggCompact in three ways — (1) '₹' prefix,
  // (2) space before Cr/L suffix (investor portal uses "₹1.23 L" style),
  // (3) en-IN grouping for values ≥1000 instead of K-compact form.
  // Investor page targets LP readers who expect natural Indian notation.
  function _fmtInr(/** @type {number|null|undefined} */ v) {
    if (v == null || !isFinite(v)) return '—';
    const abs = Math.abs(v);
    if (abs >= 10000000) return `₹${(v/10000000).toFixed(2)} Cr`;
    if (abs >= 100000)   return `₹${(v/100000).toFixed(2)} L`;
    if (abs >= 1000)     return `₹${Math.round(v).toLocaleString('en-IN')}`;
    return `₹${Math.round(v)}`;
  }
  function _fmtDate(/** @type {string|null|undefined} */ iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return formatDateIST(d, { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
    } catch { return iso; }
  }
  function _fmtPct(/** @type {number|null|undefined} */ v) {
    if (v == null || !isFinite(v)) return '—';
    return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`;
  }

  // Statement download — direct anchor since the URL itself is the
  // credential (no Bearer header). 12 months of history surfaced
  // in the dropdown; older periods can be requested by editing the
  // URL directly.
  /** Returns the last N months as {year, month, label}. The
   *  current month is excluded (statement covers a CLOSED month). */
  function _recentMonths(/** @type {number} */ n) {
    const out = [];
    const now = new Date();
    // Start from the prior month so the latest entry is the most
    // recently CLOSED period.
    let y = now.getFullYear();
    let m = now.getMonth();  // 0-indexed, so this is already "last month"
    if (m === 0) { m = 12; y -= 1; }
    for (let i = 0; i < n; i++) {
      const label = formatDateIST(new Date(y, m - 1, 1), { month: 'short', year: 'numeric' });
      out.push({ year: y, month: m, label });
      m -= 1;
      if (m < 1) { m = 12; y -= 1; }
    }
    return out;
  }
  const _months = $derived(_recentMonths(12));
  const _monthOptions = $derived(
    _months.map(m => ({ value: `${m.year}-${m.month}`, label: m.label }))
  );
  let selectedPeriod = $state(/** @type {string} */ (''));
  function _statementUrl() {
    if (!selectedPeriod) return '#';
    const [y, m] = selectedPeriod.split('-').map(Number);
    return `/api/investor/${token}/statement/${y}/${m}`;
  }
</script>

<svelte:head>
  <title>Investor Statement · RamboQuant</title>
  <meta name="robots" content="noindex,nofollow" />
</svelte:head>

<div class="ip-page">
  <header class="ip-header">
    <div class="ip-brand">
      <span class="ip-brand-mark">RAMBO</span>
      <span class="ip-brand-rest">QUANT</span>
    </div>
    <div class="ip-tag">Investor Statement</div>
  </header>

  {#if loading}
    <div class="ip-status">Loading your statement…</div>
  {:else if error}
    <div class="ip-error">
      <div class="ip-error-icon">⚠</div>
      <div class="ip-error-msg">{error}</div>
      <div class="ip-error-hint">
        If you believe this is a mistake, please contact RamboQuant for a fresh access link.
      </div>
    </div>
  {:else if slice}
    <h1 class="pub-page-heading">Investor Statement</h1>
    <section class="ip-greeting">
      Hello {slice.display_name},
    </section>

    <!-- Top stats — your slice -->
    <section class="ip-hero">
      <div class="ip-hero-row">
        <div class="ip-hero-main">
          <div class="ip-hero-lbl">Your portfolio value</div>
          <div class="ip-hero-val">{_fmtInr(slice.nav_share)}</div>
          <div class="ip-hero-asof">as of {_fmtDate(slice.as_of_date)}</div>
        </div>
        <div class="ip-hero-block"
             class:pnl-pos={(slice.pnl ?? 0) > 0}
             class:pnl-neg={(slice.pnl ?? 0) < 0}>
          <div class="ip-hero-lbl">Net Profit / Loss</div>
          <div class="ip-hero-val">
            {(slice.pnl ?? 0) >= 0 ? '+' : ''}{_fmtInr(slice.pnl)}
          </div>
          <div class="ip-hero-asof">{_fmtPct(slice.pnl_pct)}</div>
        </div>
        <div class="ip-hero-block"
             class:pnl-pos={(slice.day_delta_share ?? 0) > 0}
             class:pnl-neg={(slice.day_delta_share ?? 0) < 0}>
          <div class="ip-hero-lbl">Today's move</div>
          <div class="ip-hero-val">
            {slice.day_delta_share == null ? '—' : (slice.day_delta_share >= 0 ? '+' : '') + _fmtInr(slice.day_delta_share)}
          </div>
          <div class="ip-hero-asof">{_fmtPct(slice.day_delta_share_pct)}</div>
        </div>
      </div>
    </section>

    <!-- Composition / context -->
    <section class="ip-grid">
      <div class="ip-tile">
        <div class="ip-tile-lbl">Your contribution</div>
        <div class="ip-tile-val">{_fmtInr(slice.contribution)}</div>
      </div>
      <div class="ip-tile">
        <div class="ip-tile-lbl">Your share</div>
        <div class="ip-tile-val">{slice.share_pct.toFixed(2)}%</div>
      </div>
      <div class="ip-tile">
        <div class="ip-tile-lbl">Fund NAV (total)</div>
        <div class="ip-tile-val">{_fmtInr(slice.firm_nav)}</div>
      </div>
    </section>

    <!-- History curve -->
    {#if history.length >= 2}
      {@const _pad = { l: 50, r: 12, t: 12, b: 24 }}
      {@const W = 760}
      {@const H = 260}
      {@const innerW = W - _pad.l - _pad.r}
      {@const innerH = H - _pad.t - _pad.b}
      {@const _vals = history.map(p => p.nav_share)}
      {@const _min = Math.min(..._vals)}
      {@const _max = Math.max(..._vals)}
      {@const _range = (_max - _min) || Math.max(Math.abs(_max), 1)}
      {@const yOf = (v) => _pad.t + innerH - ((v - _min) / _range) * innerH}
      {@const xOf = (i) => _pad.l + (history.length === 1 ? innerW / 2 : (i * innerW) / (history.length - 1))}
      {@const path = history.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i)} ${yOf(p.nav_share)}`).join(' ')}
      <section class="ip-chart">
        <h2 class="ip-section-heading">Your value over time
          <span class="ip-meta">{history.length} days</span></h2>
        <svg class="ip-svg" viewBox="0 0 760 260" preserveAspectRatio="none"
             aria-label="Investor NAV history">
          {#each [0.0, 0.25, 0.5, 0.75, 1.0] as t}
            {@const y = _pad.t + innerH * t}
            {@const v = _max - _range * t}
            <line x1={_pad.l} y1={y} x2={_pad.l + innerW} y2={y}
                  stroke="#d4c89f" stroke-width="1" stroke-opacity="0.40" />
            <text x={_pad.l - 8} y={y + 3} text-anchor="end"
                  fill="#8b7340" font-size="10"
                  font-family="ui-monospace, monospace">{_fmtInr(v)}</text>
          {/each}
          <path d={path} fill="none" stroke="#d4920c" stroke-width="2" />
          <circle cx={xOf(history.length - 1)} cy={yOf(_vals[_vals.length - 1])}
                  r="3" fill="#d4920c" stroke="#fdfaf2" stroke-width="2" />
          <text x={xOf(0)} y={H - 6} text-anchor="start"
                fill="#8b7340" font-size="10"
                font-family="ui-monospace, monospace">{history[0].as_of_date}</text>
          <text x={xOf(history.length - 1)} y={H - 6} text-anchor="end"
                fill="#8b7340" font-size="10"
                font-family="ui-monospace, monospace">{history[history.length - 1].as_of_date}</text>
        </svg>
      </section>
    {/if}

    <!-- Monthly PDF statement download. Anchor (no JS fetch) since
         the URL IS the credential; the browser's "save as" picks up
         the Content-Disposition filename. -->
    <section class="ip-statement">
      <h2 class="ip-section-heading">Monthly statement</h2>
      <div class="ip-statement-row">
        <div class="ip-statement-picker">
          <Select
            bind:value={selectedPeriod}
            options={_monthOptions}
            placeholder="Pick a month…"
            theme="light"
            ariaLabel="Select statement month" />
        </div>
        <a class="ip-statement-btn"
           href={_statementUrl()}
           class:disabled={!selectedPeriod}
           aria-disabled={!selectedPeriod}
           download>
          Download PDF
        </a>
      </div>
      <div class="ip-statement-hint">
        Statements cover closed months only. If no NAV snapshot was
        recorded in that month you'll see "no data" — pick a more
        recent month.
      </div>
    </section>

    <footer class="ip-footer">
      <div class="ip-footer-disclaimer">
        This statement is for your information only. Values are
        unaudited and reflect end-of-day positions as of the date
        shown. Past performance does not guarantee future results.
      </div>
      <div class="ip-footer-meta">
        RamboQuant Analytics LLP · For questions, contact
        <a href="mailto:rambo@ramboq.com">rambo@ramboq.com</a>
      </div>
    </footer>
  {/if}
</div>

<style>
  /* Page-level reset — strip the algo / public layout chrome so the
     LP gets a clean cream/champagne canvas. */
  :global(body) { background: #fdfaf2; color: #2a2418; }

  .ip-page {
    max-width: 920px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem 4rem;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    color: #2a2418;
  }
  @media (max-width: 700px) {
    .ip-page { padding: 1.5rem 1rem 3rem; }
  }

  .ip-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    border-bottom: 1px solid #e7e0cf;
    padding-bottom: 0.9rem;
    margin-bottom: 1.6rem;
  }
  .ip-brand {
    font-weight: 800;
    letter-spacing: 0.08em;
    font-size: 1.15rem;
  }
  .ip-brand-mark { color: #d4920c; }
  .ip-brand-rest { color: #2a2418; }
  .ip-tag {
    font-size: 0.65rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #8b7340;
    font-weight: 700;
  }

  .ip-status {
    text-align: center;
    padding: 4rem 1rem;
    color: #8b7340;
    font-size: 0.85rem;
  }

  .ip-error {
    background: #fef5e7;
    border: 1px solid #e7c98e;
    border-radius: 6px;
    padding: 1.4rem 1.6rem;
    text-align: center;
  }
  .ip-error-icon {
    font-size: 1.6rem; color: #d4920c; margin-bottom: 0.3rem;
  }
  .ip-error-msg { font-weight: 700; color: #2a2418; margin-bottom: 0.4rem; }
  .ip-error-hint { font-size: 0.72rem; color: #8b7340; }

  .ip-greeting {
    font-size: 1.05rem;
    font-weight: 500;
    color: #44382a;
    margin-bottom: 1.1rem;
  }

  .ip-hero {
    background: #ffffff;
    border: 1px solid #e7e0cf;
    border-radius: 8px;
    padding: 1.3rem 1.6rem;
    margin-bottom: 1rem;
    box-shadow: 0 1px 2px rgba(212, 146, 12, 0.04);
  }
  .ip-hero-row {
    display: flex; flex-wrap: wrap; gap: 2rem;
    align-items: baseline;
  }
  .ip-hero-main { flex: 1 1 14rem; }
  .ip-hero-block { flex: 0 1 10rem; }
  .ip-hero-lbl {
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8b7340;
  }
  .ip-hero-val {
    font-size: 1.75rem;
    font-weight: 800;
    color: #2a2418;
    line-height: 1.05;
    margin-top: 0.3rem;
    font-variant-numeric: tabular-nums;
  }
  .ip-hero-main .ip-hero-val { color: #d4920c; }
  .ip-hero-block.pnl-pos .ip-hero-val { color: #14653a; }
  .ip-hero-block.pnl-neg .ip-hero-val { color: #962d2d; }
  .ip-hero-asof {
    font-size: 0.62rem;
    color: #8b7340;
    margin-top: 0.25rem;
  }

  .ip-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr));
    gap: 0.7rem;
    margin-bottom: 1.4rem;
  }
  .ip-tile {
    background: #ffffff;
    border: 1px solid #e7e0cf;
    border-radius: 6px;
    padding: 0.7rem 0.9rem;
  }
  .ip-tile-lbl {
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8b7340;
  }
  .ip-tile-val {
    margin-top: 0.25rem;
    font-size: 1rem;
    font-weight: 700;
    color: #2a2418;
    font-variant-numeric: tabular-nums;
  }

  .ip-chart {
    background: #ffffff;
    border: 1px solid #e7e0cf;
    border-radius: 8px;
    padding: 1.1rem 1.3rem;
    margin-bottom: 1.5rem;
  }
  .ip-section-heading {
    margin: 0 0 0.5rem;
    font-size: 0.7rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #44382a;
  }
  .ip-meta {
    font-weight: 500;
    color: #8b7340;
    margin-left: 0.5rem;
    text-transform: none;
    letter-spacing: 0;
  }
  .ip-svg {
    width: 100%;
    height: 240px;
    background: #fdfaf2;
    border: 1px solid #f0e6cf;
    border-radius: 6px;
  }

  .ip-statement {
    background: #ffffff;
    border: 1px solid #e7e0cf;
    border-radius: 8px;
    padding: 1.1rem 1.3rem;
    margin-bottom: 1.5rem;
  }
  .ip-statement-row {
    display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: center;
    margin-bottom: 0.5rem;
  }
  .ip-statement-picker {
    flex: 1 1 14rem;
  }
  .ip-statement-btn {
    display: inline-block;
    padding: 0.45rem 1.1rem;
    background: #d4920c;
    color: #ffffff;
    border-radius: 4px;
    text-decoration: none;
    font-weight: 700;
    font-size: 0.78rem;
    letter-spacing: 0.02em;
    transition: background 120ms;
  }
  .ip-statement-btn:hover { background: #b87b09; }
  .ip-statement-btn.disabled {
    background: #e7e0cf; color: #8b7340; pointer-events: none;
  }
  .ip-statement-hint {
    font-size: 0.65rem; color: #8b7340; line-height: 1.5;
  }

  .ip-footer {
    margin-top: 2rem;
    padding-top: 1.2rem;
    border-top: 1px solid #e7e0cf;
    font-size: 0.7rem;
    color: #8b7340;
  }
  .ip-footer-disclaimer { margin-bottom: 0.5rem; line-height: 1.55; }
  .ip-footer-meta a { color: #d4920c; text-decoration: none; }
  .ip-footer-meta a:hover { text-decoration: underline; }
</style>
