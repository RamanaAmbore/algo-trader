<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import MarketPulse from '$lib/MarketPulse.svelte';
  import PnlAnalysis from '$lib/PnlAnalysis.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import { clientTimestamp, visibleInterval } from '$lib/stores';
  import NewsList from '$lib/NewsList.svelte';
  import {
    fetchPositions, fetchHoldings, fetchRecentAgentEvents,
    fetchFunds, fetchBrokerAccounts, fetchIntradayEquity,
    batchQuote,
  } from '$lib/api';
  import { priceFmt, pctFmt, aggCompact } from '$lib/format';

  // IST-midnight-as-UTC for "today" date-window filters. Indian markets
  // (and operators) live in Asia/Kolkata; using the browser's local
  // midnight via setHours(0,0,0,0) gave wrong counts whenever the
  // browser TZ differed from IST (or even across IST midnight rollover
  // when the operator was outside India).
  function istMidnightTodayAsDate() {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Kolkata',
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date());
    const y = parts.find(p => p.type === 'year').value;
    const m = parts.find(p => p.type === 'month').value;
    const d = parts.find(p => p.type === 'day').value;
    return new Date(`${y}-${m}-${d}T00:00:00+05:30`);
  }

  // ── Demo banner — sourced from the layout's shared context ─────────
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);
  let bannerDismissed = $state(false);

  // ── Hero row state ─────────────────────────────────────────────────
  let _todayPnl     = $state(/** @type {number|null} */ (null));
  let _startingNav  = $state(/** @type {number|null} */ (null));
  let _niftyDayPct  = $state(/** @type {number|null} */ (null));
  let _firesToday   = $state(0);
  let _paperOpen    = $state(0);
  let _conn         = $state({ loaded: 0, total: 0 });
  let _heroLoadedAt = $state(/** @type {string|null} */ (null));
  let _heroTeardown;

  // Agent log collapsed by default.
  let _agentLogOpen = $state(false);
  // Operator-facing log declutter: default to agent_fire ONLY so the
  // expanded log is a thin chronological list of "what fired".
  // Operator can flip the chip to ALSO include action successes /
  // errors when they want the deeper "what did the fire DO" trace.
  let _agentLogShowActions = $state(false);
  const _agentLogKinds = $derived(
    _agentLogShowActions
      ? ['agent_fire', 'agent_action_success', 'agent_action_error']
      : ['agent_fire'],
  );

  // PnlAnalysis collapse — persisted to localStorage.
  let _pnlOpen = $state(false);


  // ── Raw positions + holdings (reused for winners/losers) ──────────
  /** @type {any[]} */
  let _positions = $state([]);
  /** @type {any[]} */
  let _holdings  = $state([]);

  // ── SymbolPanel for winners/losers tile click ──────────────────────
  let _ticketProps = $state(/** @type {any} */ (null));

  // ── Row 1: Intraday equity curve ───────────────────────────────────
  /** @type {{ ts: string, day_pnl: number, cum_pnl: number }[]} */
  let _equityPoints = $state([]);

  // ── Row 1: Margin utilisation gauges ──────────────────────────────
  /**
   * @type {{ account: string, used: number, avail: number, util_pct: number }[]}
   */
  let _margins = $state([]);

  // ── Derived hero values ────────────────────────────────────────────
  const _todayPct = $derived(
    (_todayPnl != null && _startingNav != null && _startingNav !== 0)
      ? (_todayPnl / _startingNav) * 100
      : null
  );

  const _vsNifty = $derived(
    (_todayPct != null && _niftyDayPct != null)
      ? _todayPct - _niftyDayPct
      : null
  );

  const _pnlClass = $derived(
    _todayPnl == null ? 'hero-pnl-neutral'
    : _todayPnl > 0   ? 'hero-pnl-up'
    : _todayPnl < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  const _todayPctClass = $derived(
    _todayPct == null ? 'hero-pnl-neutral'
    : _todayPct > 0   ? 'hero-pnl-up'
    : _todayPct < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  const _vsNiftyClass = $derived(
    _vsNifty == null ? 'hero-pnl-neutral'
    : _vsNifty > 0   ? 'hero-pnl-up'
    : _vsNifty < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  // ── Open orders (from layout's algoStatus poll) ───────────────────
  const _openOrders = $derived(
    /** @type {any[]} */ (algoStatus.paperStatus?.open_order_details ?? [])
  );

  // ── Winners / Losers — top-3 by P&L across positions + holdings ───
  const _combinedBook = $derived.by(() => {
    /** @type {{symbol: string, account: string, pnl: number, inv_val: number, src: string}[]} */
    const rows = [];
    for (const p of _positions) {
      const pnl = Number(p.pnl) || 0;
      if (pnl === 0) continue;
      rows.push({
        symbol:  String(p.tradingsymbol || p.symbol || ''),
        account: String(p.account || ''),
        pnl,
        inv_val: 0,
        src: 'pos',
      });
    }
    for (const h of _holdings) {
      const pnl = Number(h.day_change ?? h.day_change_pct_amount ?? 0);
      if (pnl === 0) continue;
      rows.push({
        symbol:  String(h.tradingsymbol || h.symbol || ''),
        account: String(h.account || ''),
        pnl,
        inv_val: Number(h.inv_val ?? 0),
        src: 'holding',
      });
    }
    return rows;
  });

  const _winners = $derived(
    [..._combinedBook]
      .filter(r => r.pnl > 0)
      .sort((a, b) => b.pnl - a.pnl)
      .slice(0, 3)
  );

  const _losers = $derived(
    [..._combinedBook]
      .filter(r => r.pnl < 0)
      .sort((a, b) => a.pnl - b.pnl)
      .slice(0, 3)
  );

  const _connIcon = $derived(
    _conn.total === 0     ? '—'
    : _conn.loaded === 0  ? '✗'
    : _conn.loaded < _conn.total ? '⚠'
    : '✓'
  );

  const _connClass = $derived(
    _conn.total === 0     ? 'hero-chip-conn-neutral'
    : _conn.loaded === 0  ? 'hero-chip-conn-red'
    : _conn.loaded < _conn.total ? 'hero-chip-conn-amber'
    : 'hero-chip-conn-green'
  );

  // ── Equity chart SVG constants ─────────────────────────────────────
  const CHART_W = 600;
  const CHART_H = 220;
  const PAD_L = 8;
  const PAD_R = 52;
  const PAD_T = 12;
  const PAD_B = 28;
  const INNER_W = CHART_W - PAD_L - PAD_R;
  const INNER_H = CHART_H - PAD_T - PAD_B;

  // ── Equity chart derived state ─────────────────────────────────────
  const _equityDomain = $derived.by(() => {
    if (!_equityPoints.length) return null;
    const vals = _equityPoints.map(p => p.cum_pnl);
    let yMin = Math.min(...vals);
    let yMax = Math.max(...vals);
    // ensure zero is always visible; add 10% padding
    yMin = Math.min(yMin, 0);
    yMax = Math.max(yMax, 0);
    const pad = Math.max((yMax - yMin) * 0.10, 500);
    yMin -= pad; yMax += pad;
    const ts = _equityPoints.map(p => new Date(p.ts).getTime());
    return { yMin, yMax, tMin: Math.min(...ts), tMax: Math.max(...ts) };
  });

  function _eqX(ts) {
    const d = _equityDomain;
    if (!d || d.tMax === d.tMin) return PAD_L;
    return PAD_L + ((new Date(ts).getTime() - d.tMin) / (d.tMax - d.tMin)) * INNER_W;
  }

  function _eqY(val) {
    const d = _equityDomain;
    if (!d || d.yMax === d.yMin) return PAD_T + INNER_H / 2;
    return PAD_T + (1 - (val - d.yMin) / (d.yMax - d.yMin)) * INNER_H;
  }

  const _eqPolyline = $derived.by(() => {
    if (!_equityPoints.length || !_equityDomain) return '';
    return _equityPoints.map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(p.cum_pnl).toFixed(1)}`).join(' ');
  });

  const _eqAreaPath = $derived.by(() => {
    if (!_equityPoints.length || !_equityDomain) return '';
    const pts = _equityPoints;
    const zero = _eqY(0);
    const first = `${_eqX(pts[0].ts).toFixed(1)},${zero}`;
    const last  = `${_eqX(pts[pts.length - 1].ts).toFixed(1)},${zero}`;
    const line  = pts.map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(p.cum_pnl).toFixed(1)}`).join(' L ');
    return `M ${first} L ${line} L ${last} Z`;
  });

  const _eqZeroY = $derived(_equityDomain ? _eqY(0) : null);

  const _eqPositive = $derived(
    _equityPoints.length ? _equityPoints[_equityPoints.length - 1].cum_pnl >= 0 : true
  );

  const _eqLineColor  = $derived(_eqPositive ? '#4ade80' : '#f87171');
  const _eqFillColor  = $derived(_eqPositive ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)');

  // Y-axis labels for equity chart (5 ticks)
  const _eqYLabels = $derived.by(() => {
    const d = _equityDomain;
    if (!d) return [];
    return Array.from({ length: 5 }, (_, i) => {
      const frac = i / 4;
      const val  = d.yMin + frac * (d.yMax - d.yMin);
      const y    = _eqY(val);
      return { y: y.toFixed(1), label: aggCompact(val) };
    });
  });

  // X-axis time labels (up to 5)
  const _eqXLabels = $derived.by(() => {
    const d = _equityDomain;
    if (!d || _equityPoints.length < 2) return [];
    const count = Math.min(5, _equityPoints.length);
    const step = Math.floor((_equityPoints.length - 1) / (count - 1 || 1));
    return Array.from({ length: count }, (_, i) => {
      const pt = _equityPoints[Math.min(i * step, _equityPoints.length - 1)];
      const x  = _eqX(pt.ts).toFixed(1);
      // times from backend are UTC; display in IST
      const d2  = new Date(pt.ts);
      const ist = new Date(d2.getTime() + 5.5 * 3600 * 1000);
      const ih  = String(ist.getUTCHours()).padStart(2, '0');
      const im  = String(ist.getUTCMinutes()).padStart(2, '0');
      return { x, label: `${ih}:${im}` };
    });
  });

  // Hover crosshair state
  let _hoverIdx = $state(/** @type {number|null} */ (null));
  let _hoverX   = $state(0);
  let _hoverY   = $state(0);

  const _hoverPt = $derived(
    _hoverIdx != null && _equityPoints[_hoverIdx]
      ? _equityPoints[_hoverIdx]
      : null
  );

  function _eqMouseMove(/** @type {MouseEvent} */ e) {
    if (!_equityPoints.length || !_equityDomain) return;
    const svg = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const svgX = ((e.clientX - rect.left) / rect.width) * CHART_W;
    const frac = Math.max(0, Math.min(1, (svgX - PAD_L) / INNER_W));
    const ts   = _equityDomain.tMin + frac * (_equityDomain.tMax - _equityDomain.tMin);
    // find nearest point
    let best = 0, bestDt = Infinity;
    for (let i = 0; i < _equityPoints.length; i++) {
      const dt = Math.abs(new Date(_equityPoints[i].ts).getTime() - ts);
      if (dt < bestDt) { bestDt = dt; best = i; }
    }
    _hoverIdx = best;
    _hoverX   = parseFloat(_eqX(_equityPoints[best].ts).toFixed(1));
    _hoverY   = parseFloat(_eqY(_equityPoints[best].cum_pnl).toFixed(1));
  }

  function _eqMouseLeave() { _hoverIdx = null; }

  // ── Margin gauge helpers ───────────────────────────────────────────
  const GAUGE_R = 32;
  const GAUGE_SW = 6;
  const GAUGE_CIRC = 2 * Math.PI * GAUGE_R;

  function _gaugeColor(pct) {
    if (pct < 0.50) return '#4ade80';
    if (pct < 0.70) return '#fbbf24';
    if (pct < 0.85) return '#f59410';
    return '#f87171';
  }

  function _gaugeDash(pct) {
    const used = Math.max(0, Math.min(1, pct)) * GAUGE_CIRC;
    return `${used.toFixed(2)} ${GAUGE_CIRC.toFixed(2)}`;
  }

  // ── Fetch functions ────────────────────────────────────────────────
  async function _fetchEquity() {
    try {
      const res = await fetchIntradayEquity(200);
      _equityPoints = (res?.points ?? []);
    } catch (_) { /* leave stale */ }
  }

  async function _fetchMargins() {
    try {
      const rows = await fetchFunds();
      if (!Array.isArray(rows) || !rows.length) { _margins = []; return; }
      _margins = rows
        .filter(r => r.account && !r.account.includes('TOTAL'))
        .map(r => {
          const used  = Number(r.used_margin) || 0;
          const avail = Number(r.available_margin) || 0;
          const total = used + avail;
          return {
            account: String(r.account),
            used,
            avail,
            util_pct: total > 0 ? used / total : 0,
          };
        });
    } catch (_) { _margins = []; }
  }

  async function _fetchConn() {
    try {
      const accounts = await fetchBrokerAccounts();
      if (!Array.isArray(accounts)) return;
      _conn = {
        total:  accounts.length,
        loaded: accounts.filter(a => a.loaded).length,
      };
    } catch (_) { /* leave stale */ }
  }

  async function loadHero() {
    try {
      const [positions, holdings, events] = await Promise.all([
        fetchPositions().catch(() => []),
        fetchHoldings().catch(() => []),
        fetchRecentAgentEvents(100).catch(() => []),
      ]);

      // Expose for winners/losers derivation.
      _positions = Array.isArray(positions) ? positions : [];
      _holdings  = Array.isArray(holdings)  ? holdings  : [];

      // Sum day's P&L from positions (day-pnl) + holdings (day_change).
      let dayPnl = 0;
      let invVal  = 0;
      for (const p of _positions) dayPnl += Number(p.pnl) || 0;
      for (const h of _holdings) {
        const dc = Number(h.day_change ?? h.day_change_pct_amount ?? 0);
        dayPnl += dc;
        invVal += Number(h.inv_val ?? 0);
      }
      _todayPnl    = dayPnl;
      _startingNav = invVal > 0 ? invVal : null;

      // Agent fires today (IST midnight boundary).
      const todayStart = istMidnightTodayAsDate();
      _firesToday = (events || []).filter((e) => {
        const k = e.kind ?? e.event_type ?? '';
        if (k !== 'agent_fire') return false;
        const t = new Date(e.timestamp ?? e.created_at ?? 0);
        return t >= todayStart;
      }).length;

      _paperOpen = Number(algoStatus.paperStatus?.open_order_count) || 0;
      _heroLoadedAt = clientTimestamp();

      // Parallel: equity curve + margins + conn health + NIFTY quote
      await Promise.all([
        _fetchEquity(),
        _fetchMargins(),
        _fetchConn(),
        _fetchNifty(),
      ]);
    } catch (_) { /* leave previous values up */ }
  }

  async function _fetchNifty() {
    try {
      const res = await batchQuote(['NSE:NIFTY 50']);
      const q = res?.quotes?.['NSE:NIFTY 50'] ?? res?.['NSE:NIFTY 50'] ?? null;
      if (!q) return;
      // Prefer change_percent / change_pct; fall back to (ltp-close)/close*100
      if (q.change_percent != null)     { _niftyDayPct = Number(q.change_percent); return; }
      if (q.change_pct    != null)     { _niftyDayPct = Number(q.change_pct);     return; }
      const ltp   = Number(q.last_price  ?? q.ltp  ?? 0);
      const close = Number(q.ohlc?.close ?? q.close ?? 0);
      if (close > 0 && ltp > 0) _niftyDayPct = ((ltp - close) / close) * 100;
    } catch (_) { /* leave null — chip stays "—" */ }
  }

  onMount(() => {
    bannerDismissed = localStorage.getItem('ramboq.demo_banner_dismissed') === '1';
    _pnlOpen = localStorage.getItem('dash.pnlOpen') === '1';
    loadHero();
    _heroTeardown = visibleInterval(loadHero, 30000);
  });
  onDestroy(() => { _heroTeardown?.(); });

  function dismissBanner() {
    bannerDismissed = true;
    localStorage.setItem('ramboq.demo_banner_dismissed', '1');
  }
</script>

<svelte:head>
  <title>Dashboard | RamboQuant Analytics</title>
</svelte:head>

{#if isDemo && !bannerDismissed}
  <div class="demo-banner" role="status">
    <span class="demo-banner-text">
      <strong>Live production platform</strong> · real broker data · accounts masked · paper-only writes.
      <a href="/showcase" class="demo-banner-link">Take the tour</a>
      <span class="demo-banner-sep">·</span>
      <a href="/signin" class="demo-banner-link">Sign in</a>
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

<!-- Hero row — 6 chips answering "what changed since I last looked?" -->
<div class="hero-row" role="status">
  <!-- 1. P&L TODAY -->
  <div class="hero-chip {_pnlClass}">
    <span class="hero-label">P&amp;L TODAY</span>
    <span class="hero-value">
      {#if _todayPnl == null}—{:else}{_todayPnl >= 0 ? '+' : ''}₹{priceFmt(_todayPnl)}{/if}
    </span>
  </div>

  <!-- 2. TODAY % — portfolio day return -->
  <div class="hero-chip {_todayPctClass}">
    <span class="hero-label">TODAY %</span>
    <span class="hero-value">
      {#if _todayPct == null}—{:else}{_todayPct >= 0 ? '+' : ''}{pctFmt(_todayPct)}%{/if}
    </span>
    {#if _startingNav != null}
      <span class="hero-meta">of ₹{aggCompact(_startingNav)}</span>
    {/if}
  </div>

  <!-- 3. vs NIFTY — outperformance spread -->
  <div class="hero-chip {_vsNiftyClass}">
    <span class="hero-label">vs NIFTY</span>
    <span class="hero-value">
      {#if _vsNifty == null}—{:else}{_vsNifty >= 0 ? '+' : ''}{pctFmt(_vsNifty)}%{/if}
    </span>
    {#if _niftyDayPct != null}
      <span class="hero-meta">NIFTY {_niftyDayPct >= 0 ? '+' : ''}{pctFmt(_niftyDayPct)}%</span>
    {/if}
  </div>

  <!-- 4. AGENT FIRES -->
  <div class="hero-chip hero-chip-fires">
    <span class="hero-label">AGENT FIRES</span>
    <span class="hero-value">{_firesToday}</span>
    <span class="hero-meta">today</span>
  </div>

  <!-- 5. PAPER OPEN -->
  <div class="hero-chip hero-chip-paper">
    <span class="hero-label">PAPER OPEN</span>
    <span class="hero-value">{_paperOpen}</span>
    <span class="hero-meta">orders</span>
  </div>

  <!-- 6. CONN — broker connection health -->
  <div class="hero-chip hero-chip-conn {_connClass}">
    <span class="hero-label">CONN</span>
    <span class="hero-value conn-icon">{_connIcon}</span>
    {#if _conn.total > 0}
      <span class="hero-meta">{_conn.loaded}/{_conn.total}</span>
    {/if}
  </div>

  {#if _heroLoadedAt}
    <span class="hero-refresh">refreshed {_heroLoadedAt}</span>
  {/if}
</div>

<!-- Open orders strip — hidden when nothing is chasing -->
{#if _openOrders.length > 0}
  <div class="dash-open-orders">
    <div class="oo-header">
      <span class="mp-section-label">OPEN ORDERS</span>
      <span class="oo-count">
        <span class="oo-dot" aria-hidden="true"></span>
        {_openOrders.length} chasing
      </span>
    </div>
    <div class="oo-pills">
      {#each _openOrders as ord}
        {@const isBuy = (ord.side ?? '').toUpperCase() === 'BUY'}
        <a
          href="/orders{ord.order_id ? `?order_id=${encodeURIComponent(ord.order_id)}` : ''}"
          class="oo-pill {isBuy ? 'oo-pill-buy' : 'oo-pill-sell'}"
        >
          <span class="oo-side">{isBuy ? 'BUY' : 'SELL'}</span>
          <span class="oo-qty">{ord.qty ?? ord.quantity ?? ''}</span>
          <span class="oo-sym">{ord.symbol ?? ord.tradingsymbol ?? ''}</span>
          <span class="oo-price">@ ₹{priceFmt(ord.limit_price ?? ord.price ?? 0)}</span>
          {#if (ord.attempts ?? 0) > 0}
            <span class="oo-attempts">({ord.attempts})</span>
          {/if}
        </a>
      {/each}
    </div>
  </div>
{/if}

<!-- Row 1: Intraday equity curve (left) + Margin gauges (right) -->
<div class="dash-row1">
  <!-- Left: Intraday equity curve -->
  <section class="row1-col row1-col-chart">
    <div class="mp-section-label">Intraday Equity Curve</div>
    {#if !_equityPoints.length}
      <div class="eq-empty">
        No data yet — markets open at 09:15 IST
      </div>
    {:else}
      <svg
        class="eq-svg"
        viewBox="0 0 {CHART_W} {CHART_H}"
        preserveAspectRatio="none"
        role="img"
        aria-label="Intraday cumulative P&L curve"
        onmousemove={_eqMouseMove}
        onmouseleave={_eqMouseLeave}
      >
        <!-- Grid lines (horizontal) -->
        {#each [0.0, 0.25, 0.5, 0.75, 1.0] as frac}
          {@const gy = PAD_T + frac * INNER_H}
          <line
            x1={PAD_L} y1={gy} x2={PAD_L + INNER_W} y2={gy}
            stroke="rgba(200,216,240,0.10)" stroke-width="1" />
        {/each}

        <!-- Zero baseline (dotted) -->
        {#if _eqZeroY != null}
          <line
            x1={PAD_L} y1={_eqZeroY} x2={PAD_L + INNER_W} y2={_eqZeroY}
            stroke="rgba(200,216,240,0.45)" stroke-width="1"
            stroke-dasharray="4 3" />
        {/if}

        <!-- Filled area -->
        {#if _eqAreaPath}
          <path d={_eqAreaPath} fill={_eqFillColor} />
        {/if}

        <!-- Line -->
        {#if _eqPolyline}
          <polyline
            points={_eqPolyline}
            fill="none"
            stroke={_eqLineColor}
            stroke-width="1.5"
            stroke-linejoin="round"
            stroke-linecap="round" />
        {/if}

        <!-- Y-axis labels (right) -->
        {#each _eqYLabels as lbl}
          <text
            x={PAD_L + INNER_W + 4} y={parseFloat(lbl.y) + 3.5}
            font-size="9" fill="#7e97b8" font-family="ui-monospace,monospace"
            text-anchor="start">{lbl.label}</text>
        {/each}

        <!-- X-axis labels -->
        {#each _eqXLabels as lbl}
          <text
            x={parseFloat(lbl.x)} y={CHART_H - 6}
            font-size="9" fill="#7e97b8" font-family="ui-monospace,monospace"
            text-anchor="middle">{lbl.label}</text>
        {/each}

        <!-- Hover crosshair -->
        {#if _hoverPt != null}
          <line
            x1={_hoverX} y1={PAD_T} x2={_hoverX} y2={PAD_T + INNER_H}
            stroke="rgba(200,216,240,0.55)" stroke-width="1"
            stroke-dasharray="3 2" />
          <circle cx={_hoverX} cy={_hoverY} r="3"
            fill={_eqLineColor} stroke="#0a1428" stroke-width="1.5" />
          <!-- Tooltip box -->
          {@const _tipX = _hoverX > INNER_W * 0.65 ? _hoverX - 108 : _hoverX + 8}
          {@const _tipY = Math.max(PAD_T, Math.min(_hoverY - 28, PAD_T + INNER_H - 58))}
          <rect x={_tipX} y={_tipY} width="100" height="54"
            rx="3" fill="rgba(10,20,40,0.92)"
            stroke="rgba(126,151,184,0.35)" stroke-width="1" />
          {#if _hoverPt}
            {@const _ist = new Date(new Date(_hoverPt.ts).getTime() + 5.5*3600*1000)}
            {@const _th = String(_ist.getUTCHours()).padStart(2,'0')}
            {@const _tm = String(_ist.getUTCMinutes()).padStart(2,'0')}
            <text x={_tipX + 6} y={_tipY + 13}
              font-size="8.5" fill="#7dd3fc" font-family="ui-monospace,monospace">{_th}:{_tm} IST</text>
            <text x={_tipX + 6} y={_tipY + 26}
              font-size="8" fill="#7e97b8" font-family="ui-monospace,monospace">Day P&amp;L</text>
            <text x={_tipX + 6} y={_tipY + 37}
              font-size="9" font-weight="700" fill={_hoverPt.day_pnl >= 0 ? '#4ade80' : '#f87171'}
              font-family="ui-monospace,monospace"
              style="font-variant-numeric:tabular-nums">
              {_hoverPt.day_pnl >= 0 ? '+' : ''}₹{priceFmt(_hoverPt.day_pnl)}
            </text>
            <text x={_tipX + 6} y={_tipY + 49}
              font-size="9" font-weight="700" fill={_hoverPt.cum_pnl >= 0 ? '#4ade80' : '#f87171'}
              font-family="ui-monospace,monospace"
              style="font-variant-numeric:tabular-nums">
              cum {_hoverPt.cum_pnl >= 0 ? '+' : ''}₹{priceFmt(_hoverPt.cum_pnl)}
            </text>
          {/if}
        {/if}
      </svg>
    {/if}
  </section>

  <!-- Right: Margin utilisation gauges -->
  <section class="row1-col row1-col-gauges">
    <div class="mp-section-label">Margin Utilisation</div>
    {#if !_margins.length}
      <div class="gauge-empty">No accounts connected</div>
    {:else}
      <div class="gauge-grid">
        {#each _margins as acct}
          {@const color = _gaugeColor(acct.util_pct)}
          {@const dash  = _gaugeDash(acct.util_pct)}
          <div class="gauge-tile">
            <svg class="gauge-svg" viewBox="0 0 80 80" width="80" height="80"
              role="img" aria-label="{acct.account} margin utilisation {(acct.util_pct*100).toFixed(0)}%">
              <!-- Track -->
              <circle
                cx="40" cy="40" r={GAUGE_R}
                fill="none"
                stroke="rgba(126,151,184,0.18)"
                stroke-width={GAUGE_SW} />
              <!-- Arc — starts at top (−90 deg) via transform -->
              <circle
                cx="40" cy="40" r={GAUGE_R}
                fill="none"
                stroke={color}
                stroke-width={GAUGE_SW}
                stroke-dasharray={dash}
                stroke-linecap="round"
                transform="rotate(-90 40 40)" />
              <!-- Percentage label inside -->
              <text x="40" y="44" text-anchor="middle"
                font-size="13" font-weight="800"
                font-family="ui-monospace,monospace"
                style="font-variant-numeric:tabular-nums"
                fill={color}>{(acct.util_pct * 100).toFixed(0)}%</text>
            </svg>
            <span class="gauge-label">{acct.account}</span>
            <span class="gauge-detail">
              ₹{aggCompact(acct.used)} / ₹{aggCompact(acct.used + acct.avail)}
            </span>
          </div>
        {/each}
      </div>
    {/if}
  </section>
</div>

<!-- Row 2: Top winners (left) + Top losers (right) — hidden when book is empty -->
{#if _winners.length > 0 || _losers.length > 0}
  <div class="dash-row2">
    <!-- Winners tile -->
    {#if _winners.length > 0}
      <section class="wl-tile wl-tile-win">
        <div class="mp-section-label wl-tile-label">TOP WINNERS</div>
        <div class="wl-rows">
          {#each _winners as row}
            <button
              class="wl-row"
              onclick={() => {
                const sym = row.symbol.trim();
                if (!sym) return;
                _ticketProps = {
                  symbol:     sym,
                  defaultTab: 'chart',
                  onClose:    () => { _ticketProps = null; },
                  onSubmit:   () => { _ticketProps = null; },
                };
              }}
            >
              <span class="wl-sym">{row.symbol}</span>
              <span class="wl-pnl wl-pnl-up">+₹{priceFmt(row.pnl)}</span>
              {#if row.inv_val > 0}
                <span class="wl-pct">({pctFmt((row.pnl / row.inv_val) * 100)}%)</span>
              {/if}
            </button>
          {/each}
        </div>
      </section>
    {/if}

    <!-- Losers tile -->
    {#if _losers.length > 0}
      <section class="wl-tile wl-tile-loss">
        <div class="mp-section-label wl-tile-label">TOP LOSERS</div>
        <div class="wl-rows">
          {#each _losers as row}
            <button
              class="wl-row"
              onclick={() => {
                const sym = row.symbol.trim();
                if (!sym) return;
                _ticketProps = {
                  symbol:     sym,
                  defaultTab: 'chart',
                  onClose:    () => { _ticketProps = null; },
                  onSubmit:   () => { _ticketProps = null; },
                };
              }}
            >
              <span class="wl-sym">{row.symbol}</span>
              <span class="wl-pnl wl-pnl-down">-₹{priceFmt(Math.abs(row.pnl))}</span>
              {#if row.inv_val > 0}
                <span class="wl-pct">({pctFmt((row.pnl / row.inv_val) * 100)}%)</span>
              {/if}
            </button>
          {/each}
        </div>
      </section>
    {/if}
  </div>
{/if}

<!-- Row 3: Market news strip — single column. -->
<div class="dash-row3">
  <div class="row3-header">
    <span class="mp-section-label">MARKET NEWS</span>
  </div>
  <NewsList limit={5} showRefreshTime={true} />
</div>

<!-- Two-column grid (≥1200px): P&L Analysis on the left, MarketPulse
     stack (Funds + Positions Summary + Holdings Summary) on the right. -->
<div class="dash-grid">
  <section class="dash-col dash-col-pnl">
    <!-- P&L Analysis — collapsed by default; state persisted to localStorage. -->
    <details
      class="dash-pnl-details"
      bind:open={_pnlOpen}
      ontoggle={() => localStorage.setItem('dash.pnlOpen', _pnlOpen ? '1' : '0')}
    >
      <summary class="dash-pnl-summary">
        <span class="mp-section-label">P&amp;L ANALYSIS</span>
        <span class="dash-pnl-toggle">{_pnlOpen ? '▾ collapse' : '▸ expand'}</span>
      </summary>
      <div class="dash-pnl-body">
        <PnlAnalysis />
      </div>
    </details>
  </section>
  <section class="dash-col dash-col-pulse">
    <!-- Dashboard scope: positions + holdings only. Funds, watchlist,
         pinned, and movers are all off — those buckets don't belong
         on the dashboard and shouldn't clutter the source-picker
         either. Source toggles stay on so the operator can still
         show positions-only or holdings-only, but with just those
         two options surfaced. -->
    <!-- Dashboard scope: Funds + Positions + Holdings summary, three
         compact grids. Source-toggle MultiSelect stays off (the
         watchlist / movers / pinned filters don't apply when only
         the summary grids render). Funds was briefly dropped per a
         scope clarification, then restored — operator wants the
         margin/cash glance alongside positions+holdings on /dashboard. -->
    <MarketPulse
      title="Performance"
      enableWatchlists={false}
      enableMovers={false}
      enablePinned={false}
      enableSourceToggles={false}
      allowOrders={true}
      accountPicker={true}
      showSummary={true}
      showFunds={true}
      showSymbolsGrid={false} />
  </section>
</div>

<!-- SymbolPanel — opened by winners/losers tile clicks -->
{#if _ticketProps}
  <SymbolPanel
    {..._ticketProps}
    onClose={() => { _ticketProps = null; }}
    onSubmit={() => { _ticketProps = null; }} />
{/if}

<!-- Agent activity — collapsed by default. Expands to a clean
     fires-only log; chip flips to also show action successes/errors
     for the deeper "what did the fire actually do" trace. -->
<details class="dash-agent" bind:open={_agentLogOpen}>
  <summary class="dash-agent-summary">
    <span class="mp-section-label">Agent activity</span>
    <span class="dash-agent-chip">
      <span class="dash-agent-count">{_firesToday}</span>
      <span class="dash-agent-label">fires today</span>
    </span>
    <span class="dash-agent-toggle">{_agentLogOpen ? '▾ hide log' : '▸ show log'}</span>
  </summary>
  <!-- Inline filter chip — flips fires-only vs fires+actions. Click
       handler stops the click from bubbling up to the <summary>
       (which would toggle the details element instead). -->
  <div class="dash-agent-filter">
    <button
      type="button"
      class="dash-agent-filter-btn"
      class:dash-agent-filter-btn-on={_agentLogShowActions}
      onclick={(e) => { e.preventDefault(); e.stopPropagation();
                        _agentLogShowActions = !_agentLogShowActions; }}>
      {_agentLogShowActions ? '✓' : ''} include action events
    </button>
    <span class="dash-agent-filter-hint">
      {_agentLogShowActions
        ? 'showing fires + action successes/errors'
        : 'showing fires only — toggle to include actions'}
    </span>
  </div>
  <UnifiedLog
    filter={{ kinds: _agentLogKinds }}
    excludeSim={true}
    maxRows={30}
    emptyMessage="No agent fires yet today." />
</details>

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

  /* Section labels — used as the heading inside every dashboard card
     (Intraday Equity Curve, Margin Utilisation, Top Winners, Top
     Losers, Market News, P&L Analysis, Agent activity, OPEN ORDERS).
     Treatment: amber accent bar on the left + amber small-caps text.
     Reads as a classic trader-platform "section tag" — distinct from
     the body but tasteful, not shouting. */
  .mp-section-label {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.68rem;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #fbbf24;
    margin-bottom: 0.45rem;
    padding: 0.05rem 0;
  }
  .mp-section-label::before {
    content: '';
    display: inline-block;
    width: 3px;
    height: 0.85rem;
    background: linear-gradient(180deg, #fbbf24 0%, #f59e0b 100%);
    border-radius: 1px;
    flex-shrink: 0;
    box-shadow: 0 0 6px rgba(251, 191, 36, 0.45);
  }

  /* ── Hero row ────────────────────────────────────────────────────── */
  /* All card-shaped sections on this page (hero row, row1 cols, wl
     tiles, news strip, collapsible summaries) inherit the canonical
     algo-status-card chrome — gradient bg + 1.5px border + box-shadow.
     Match the visual depth of /agents, /admin/options, /admin/execution
     so the dashboard doesn't read as one-generation-back. */
  /* Hide the CONN chip on narrow viewports — it tends to wrap onto a
     row of its own (single chip, looks orphaned) and "no accounts
     connected" has no actionable meaning for a demo / recruiter
     visitor scanning the dashboard on a phone. Operators on desktop
     keep it as a glanceable broker-health indicator. */
  @media (max-width: 600px) {
    .hero-chip.hero-chip-conn { display: none; }
  }
  .hero-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.6rem;
    margin: 0 0 0.6rem 0;
    padding: 0.5rem 0.7rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
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
  .hero-pnl-up      { border-left-color: #4ade80; }
  .hero-pnl-up      .hero-value { color: #4ade80; }
  .hero-pnl-down    { border-left-color: #f87171; }
  .hero-pnl-down    .hero-value { color: #f87171; }
  .hero-pnl-neutral { border-left-color: #7e97b8; }
  .hero-chip-fires  { border-left-color: #fbbf24; }
  .hero-chip-paper  { border-left-color: #7dd3fc; }

  /* CONN chip — border driven by conn state class */
  .hero-chip-conn { border-left-color: #7e97b8; }
  .hero-chip-conn-green { border-left-color: #4ade80; }
  .hero-chip-conn-green .hero-value,
  .hero-chip-conn-green .conn-icon { color: #4ade80; }
  .hero-chip-conn-amber { border-left-color: #fbbf24; }
  .hero-chip-conn-amber .hero-value,
  .hero-chip-conn-amber .conn-icon { color: #fbbf24; }
  .hero-chip-conn-red   { border-left-color: #f87171; }
  .hero-chip-conn-red   .hero-value,
  .hero-chip-conn-red   .conn-icon { color: #f87171; }
  .hero-chip-conn-neutral { border-left-color: #7e97b8; }

  .hero-refresh {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
  }

  /* ── Row 1: equity curve + margin gauges ─────────────────────────── */
  .dash-row1 {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.75rem;
    margin-bottom: 0.6rem;
  }
  @media (min-width: 1024px) {
    .dash-row1 {
      grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
      gap: 1rem;
      align-items: start;
    }
  }
  .row1-col {
    min-width: 0;
    padding: 0.65rem 0.75rem 0.6rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
  }

  /* Equity curve */
  .eq-svg {
    display: block;
    width: 100%;
    height: 220px;
    cursor: crosshair;
    overflow: visible;
  }
  .eq-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 220px;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    letter-spacing: 0.04em;
  }

  /* Margin gauges */
  .gauge-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100px;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    letter-spacing: 0.04em;
  }
  .gauge-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 1.2rem 1.5rem;
    padding: 0.4rem 0 0.2rem;
    justify-content: center;
  }
  .gauge-tile {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
  }
  .gauge-svg {
    display: block;
    flex-shrink: 0;
  }
  .gauge-label {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    color: #7e97b8;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .gauge-detail {
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.03em;
  }

  /* ── Two-column dash-grid (below row1) ──────────────────────────── */
  .dash-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.75rem;
    margin-bottom: 0.6rem;
  }
  @media (min-width: 1200px) {
    .dash-grid {
      grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
      gap: 1rem;
      align-items: start;
    }
  }
  .dash-col       { min-width: 0; }
  .dash-col-pnl   { display: flex; flex-direction: column; }
  .dash-col-pulse { display: flex; flex-direction: column; }

  /* Agent log */
  .dash-agent {
    margin-top: 0.6rem;
  }
  .dash-agent-summary {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    cursor: pointer;
    list-style: none;
    user-select: none;
    padding: 0.5rem 0.7rem;
    border-radius: 6px;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .dash-agent-summary::-webkit-details-marker { display: none; }
  .dash-agent-summary:hover {
    border-color: rgba(251, 191, 36, 0.50);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  .dash-agent[open] > .dash-agent-summary {
    border-color: rgba(251, 191, 36, 0.65);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  .dash-agent-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    padding: 0.1rem 0.5rem;
    border-left: 2px solid #fbbf24;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 2px;
    font-family: ui-monospace, monospace;
    line-height: 1;
  }
  .dash-agent-count {
    color: #fbbf24;
    font-size: 0.85rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .dash-agent-label {
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .dash-agent-toggle {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    letter-spacing: 0.04em;
  }
  /* Inline filter strip inside the expanded agent-activity log.
     The chip on the left is a toggleable pill; the hint on the
     right just describes what the operator is currently looking at. */
  .dash-agent-filter {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.35rem 0.5rem;
    margin-top: 0.35rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.15);
    font-family: ui-monospace, monospace;
  }
  .dash-agent-filter-btn {
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.25);
    color: #c8d8f0;
    font-family: inherit;
    font-size: 0.6rem;
    padding: 0.18rem 0.5rem;
    border-radius: 3px;
    cursor: pointer;
    transition: background-color 0.15s, color 0.15s;
  }
  .dash-agent-filter-btn:hover {
    background: rgba(251, 191, 36, 0.10);
    color: #fbbf24;
  }
  .dash-agent-filter-btn-on {
    background: rgba(251, 191, 36, 0.18);
    border-color: rgba(251, 191, 36, 0.55);
    color: #fbbf24;
  }
  .dash-agent-filter-hint {
    color: rgba(126, 151, 184, 0.65);
    font-size: 0.56rem;
    letter-spacing: 0.02em;
  }
  .pnl-section-label {
    margin-top: 0.75rem;
    margin-bottom: 0.3rem;
  }

  /* ── Open orders strip ───────────────────────────────────────────── */
  .dash-open-orders {
    margin-bottom: 0.6rem;
    padding: 0.4rem 0.55rem;
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .oo-header {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    margin-bottom: 0.35rem;
  }
  .oo-count {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    color: #7dd3fc;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .oo-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #7dd3fc;
    animation: oo-pulse 2s ease-in-out infinite;
  }
  @keyframes oo-pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }
  .oo-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }
  .oo-pill {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    padding: 0.2rem 0.5rem;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    text-decoration: none;
    border: 1px solid;
    font-variant-numeric: tabular-nums;
    transition: filter 0.12s;
    white-space: nowrap;
  }
  .oo-pill:hover { filter: brightness(1.15); }
  .oo-pill-buy {
    background: rgba(74, 222, 128, 0.10);
    border-color: rgba(74, 222, 128, 0.30);
    color: #a7f3c0;
  }
  .oo-pill-sell {
    background: rgba(248, 113, 113, 0.10);
    border-color: rgba(248, 113, 113, 0.30);
    color: #fca5a5;
  }
  .oo-side   { font-weight: 800; font-size: 0.58rem; letter-spacing: 0.06em; }
  .oo-qty    { font-weight: 700; }
  .oo-sym    { font-weight: 700; }
  .oo-price  { color: #c8d8f0; }
  .oo-attempts { color: #7e97b8; font-size: 0.58rem; }

  /* ── Row 2: Winners / Losers ─────────────────────────────────────── */
  .dash-row2 {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.75rem;
    margin-bottom: 0.6rem;
  }
  @media (min-width: 1024px) {
    .dash-row2 {
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
    }
  }
  .wl-tile {
    padding: 0.65rem 0.75rem 0.6rem;
    border-radius: 6px;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-top-width: 3px;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    min-width: 0;
  }
  /* Coloured top accent on each winner/loser tile — same idiom as
     /showcase cards. Identity is in the border-top stripe, not in
     the body tint, so the tiles still belong to the algo card family. */
  .wl-tile-win  { border-top-color: rgba(74, 222, 128, 0.85); }
  .wl-tile-loss { border-top-color: rgba(248, 113, 113, 0.85); }
  .wl-tile-label {
    margin-bottom: 0.35rem;
  }
  .wl-rows {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .wl-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    width: 100%;
    padding: 0.22rem 0.3rem;
    border-radius: 3px;
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    font-family: ui-monospace, monospace;
    transition: background 0.1s;
  }
  .wl-row:hover { background: rgba(255, 255, 255, 0.04); }
  .wl-sym {
    font-size: 0.72rem;
    font-weight: 700;
    color: #e2ecff;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wl-pnl {
    font-size: 0.75rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .wl-pnl-up   { color: #4ade80; }
  .wl-pnl-down { color: #f87171; }
  .wl-pct {
    font-size: 0.6rem;
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  /* ── Row 3: Market news strip ───────────────────────────────────── */
  .dash-row3 {
    margin-bottom: 0.6rem;
    padding: 0.55rem 0.75rem 0.6rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
  }
  .row3-header {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    margin-bottom: 0.3rem;
  }

  /* ── P&L Analysis collapsible ────────────────────────────────────── */
  /* Summary bar carries the same card chrome as every other section
     even when collapsed, so the surface reads as a closed accordion
     panel — not a hairline. Hover lifts the border to amber as before. */
  .dash-pnl-summary {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    cursor: pointer;
    list-style: none;
    user-select: none;
    padding: 0.5rem 0.7rem;
    border-radius: 6px;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .dash-pnl-summary::-webkit-details-marker { display: none; }
  .dash-pnl-summary:hover {
    border-color: rgba(251, 191, 36, 0.50);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  /* Open state — amber accent stays so the operator knows which
     section is currently exposing its inner content. */
  .dash-pnl-details[open] > .dash-pnl-summary {
    border-color: rgba(251, 191, 36, 0.65);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                0 0 0 1px rgba(251, 191, 36, 0.18);
  }
  .dash-pnl-toggle {
    margin-left: auto;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    letter-spacing: 0.04em;
  }
  .dash-pnl-body {
    margin-top: 0.4rem;
  }

  /* Demo banner */
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
  .demo-banner-sep { color: rgba(168,85,247,0.45); margin: 0 0.35rem; }
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
