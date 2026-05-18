<script>
  // EquityCurve — cumulative P&L over time as one SVG line. Built
  // for the Lab simulator: an "equity curve" view that answers
  // "did this strategy make money over the run?" without needing
  // a separate analytics page. Industry-standard chart on any
  // backtest / forward-test tool (AlgoTest, QuantConnect, NinjaTrader,
  // TradingView Strategy Tester).
  //
  // Y-axis: raw ₹ P&L, with a horizontal reference line at 0 so the
  // operator can see "above zero = winning, below = losing" at a
  // glance. X-axis: timestamps from each captured tick.

  import { priceFmt } from '$lib/format';

  /** @type {{
   *   ticks?:      Array<{ts: string, pnl: number}>,
   *   height?:     number,
   *   title?:      string,
   *   scrubbedTs?: string | null,
   * }} */
  const { ticks = [], height = 180, title = 'Equity curve',
          scrubbedTs = null } = $props();

  const W       = 720;
  const PAD_L   = 56;
  const PAD_R   = 16;
  const PAD_T   = 12;
  const PAD_B   = 28;
  const innerW  = $derived(W - PAD_L - PAD_R);
  const innerH  = $derived(height - PAD_T - PAD_B);

  // ── Domains ──────────────────────────────────────────────────────
  const xDomain = $derived.by(() => {
    if (!ticks.length) return null;
    const ts = ticks.map((t) => Date.parse(t.ts)).filter(Number.isFinite);
    if (!ts.length) return null;
    const lo = Math.min(...ts), hi = Math.max(...ts);
    if (hi === lo) return { lo: lo - 500, hi: hi + 500 };
    return { lo, hi };
  });

  // Always include 0 in the y-domain so the reference line is visible.
  // Pad by 10 % of the range so the top/bottom values don't sit flush
  // against the axis frame.
  const yDomain = $derived.by(() => {
    if (!ticks.length) return { lo: -1, hi: 1 };
    const pnls = ticks.map((t) => Number(t.pnl) || 0);
    let lo = Math.min(0, ...pnls);
    let hi = Math.max(0, ...pnls);
    const pad = Math.max((hi - lo) * 0.1, 1);
    return { lo: lo - pad, hi: hi + pad };
  });

  const xOf = (/** @type {number} */ tMs) => {
    if (!xDomain) return PAD_L;
    return PAD_L + ((tMs - xDomain.lo) / (xDomain.hi - xDomain.lo)) * innerW;
  };
  const yOf = (/** @type {number} */ v) => {
    return PAD_T + ((yDomain.hi - v) / (yDomain.hi - yDomain.lo)) * innerH;
  };

  // ── Line + zero ref ──────────────────────────────────────────────
  const linePath = $derived.by(() => {
    if (!ticks.length || !xDomain) return '';
    let d = '';
    for (let i = 0; i < ticks.length; i++) {
      const t = Date.parse(ticks[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = xOf(t);
      const y = yOf(Number(ticks[i].pnl) || 0);
      d += (i === 0 ? `M${x.toFixed(2)},${y.toFixed(2)}`
                    : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  });

  // Fill polygon — area between the line and zero (green above,
  // red below). Two paths so each side gets its own fill colour.
  // Both polygons are clipped at y=yOf(0) so the operator sees a
  // sharp transition where P&L crosses zero.
  const filledArea = $derived.by(() => {
    if (!ticks.length || !xDomain) return '';
    let d = '';
    for (let i = 0; i < ticks.length; i++) {
      const t = Date.parse(ticks[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = xOf(t);
      const y = yOf(Number(ticks[i].pnl) || 0);
      d += (i === 0 ? `M${x.toFixed(2)},${yOf(0).toFixed(2)} L${x.toFixed(2)},${y.toFixed(2)}`
                    : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    // Close back to zero.
    const lastT = Date.parse(ticks[ticks.length - 1].ts);
    if (Number.isFinite(lastT)) d += ` L${xOf(lastT).toFixed(2)},${yOf(0).toFixed(2)} Z`;
    return d;
  });

  // ── Y-axis ticks (5 evenly spaced) + 0 always included ───────────
  const yTicks = $derived.by(() => {
    if (!ticks.length) return [];
    const out = [];
    const step = (yDomain.hi - yDomain.lo) / 4;
    for (let i = 0; i <= 4; i++) out.push(yDomain.lo + i * step);
    // Force a tick at 0 (rounding error tolerance).
    if (!out.some((v) => Math.abs(v) < step * 0.05)) out.push(0);
    return out.sort((a, b) => a - b);
  });

  // ── X-axis labels (4 evenly spaced HH:MM:SS) ─────────────────────
  const xLabels = $derived.by(() => {
    if (!xDomain) return [];
    const out = [];
    for (let i = 0; i < 4; i++) {
      const t = xDomain.lo + ((xDomain.hi - xDomain.lo) * i) / 3;
      const d = new Date(t);
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      out.push({ x: xOf(t), label: `${hh}:${mm}:${ss}` });
    }
    return out;
  });

  // ── Hover crosshair — find nearest tick to pointer x ─────────────
  /** @type {{x:number,y:number,ts:string,pnl:number}|null} */
  let hover = $state(null);
  function onPointerMove(/** @type {PointerEvent} */ ev) {
    if (!ticks.length || !xDomain) { hover = null; return; }
    const svg = /** @type {SVGSVGElement} */ (ev.currentTarget);
    const rect = svg.getBoundingClientRect();
    const xRel = ((ev.clientX - rect.left) / rect.width) * W;
    const tMs = xDomain.lo + ((xRel - PAD_L) / innerW) * (xDomain.hi - xDomain.lo);
    let best = ticks[0], bestD = Infinity;
    for (const t of ticks) {
      const d = Math.abs(Date.parse(t.ts) - tMs);
      if (d < bestD) { bestD = d; best = t; }
    }
    const tx = Date.parse(best.ts);
    hover = { x: xOf(tx), y: yOf(Number(best.pnl) || 0),
              ts: best.ts, pnl: Number(best.pnl) || 0 };
  }
  function onPointerLeave() { hover = null; }

  // ── Final P&L value for the header chip ──────────────────────────
  const finalPnl = $derived(ticks.length
    ? Number(ticks[ticks.length - 1].pnl) || 0
    : 0);
  const pnlClass = $derived(
    finalPnl > 0 ? 'eq-final-up'
    : finalPnl < 0 ? 'eq-final-down' : 'eq-final-flat'
  );
</script>

<div class="eq-shell">
  <div class="eq-header-row">
    {#if title}
      <div class="eq-header">{title}</div>
    {/if}
    {#if ticks.length}
      <span class="eq-final {pnlClass}">
        {finalPnl >= 0 ? '+' : ''}₹{priceFmt(finalPnl)}
      </span>
    {/if}
  </div>

  {#if !ticks.length}
    <div class="eq-empty">No P&amp;L history yet. Start the sim to populate.</div>
  {:else}
    <svg viewBox="0 0 {W} {height}" preserveAspectRatio="none"
         class="eq-svg" onpointermove={onPointerMove} onpointerleave={onPointerLeave}>
      <!-- Filled area (green above zero, red below) -->
      <path d={filledArea}
            fill="url(#eq-grad-up)"
            opacity="0.18" />
      <defs>
        <linearGradient id="eq-grad-up" x1="0" y1="0" x2="0" y2={height}
                        gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#4ade80" />
          <stop offset={`${(yOf(0) / height) * 100}%`} stop-color="#4ade80" />
          <stop offset={`${(yOf(0) / height) * 100}%`} stop-color="#f87171" />
          <stop offset="100%" stop-color="#f87171" />
        </linearGradient>
      </defs>

      <!-- y-axis grid + labels (₹) -->
      {#each yTicks as v}
        <line x1={PAD_L} x2={W - PAD_R} y1={yOf(v)} y2={yOf(v)}
              stroke={Math.abs(v) < 0.001 ? 'rgba(200,216,240,0.30)' : 'rgba(200,216,240,0.08)'}
              stroke-width={Math.abs(v) < 0.001 ? 1 : 0.7}
              stroke-dasharray={Math.abs(v) < 0.001 ? '' : '2 3'} />
        <text x={PAD_L - 6} y={yOf(v) + 3} text-anchor="end"
              fill={Math.abs(v) < 0.001 ? '#c8d8f0' : '#7e97b8'}
              font-size="10"
              font-weight={Math.abs(v) < 0.001 ? 700 : 500}>
          {v >= 0 ? '+' : '−'}₹{priceFmt(Math.abs(v))}
        </text>
      {/each}

      <!-- x-axis labels -->
      {#each xLabels as l}
        <line x1={l.x} x2={l.x} y1={PAD_T} y2={height - PAD_B}
              stroke="rgba(200,216,240,0.07)" stroke-width="0.7" stroke-dasharray="2 3" />
        <text x={l.x} y={height - PAD_B + 14} text-anchor="middle"
              fill="#7e97b8" font-size="10">{l.label}</text>
      {/each}

      <!-- Line -->
      <path d={linePath} fill="none"
            stroke="#fbbf24" stroke-width="1.8"
            stroke-linejoin="round" stroke-linecap="round" />

      <!-- Replay-scrubber anchor (shared across all Lab charts via
           the scrubbedTs prop). When set and not over-ridden by a
           live hover, draws a vertical amber dashed line at the
           scrubbed timestamp so the operator can read the equity
           value at that historical moment. -->
      {#if scrubbedTs && !hover}
        {@const _stMs = Date.parse(scrubbedTs)}
        {#if Number.isFinite(_stMs) && xDomain}
          <line x1={xOf(_stMs)} x2={xOf(_stMs)} y1={PAD_T} y2={height - PAD_B}
                stroke="rgba(251,191,36,0.7)" stroke-width="1.25"
                stroke-dasharray="4 3" />
        {/if}
      {/if}

      <!-- Hover crosshair + dot + value label -->
      {#if hover}
        <line x1={hover.x} x2={hover.x} y1={PAD_T} y2={height - PAD_B}
              stroke="rgba(251,191,36,0.5)" stroke-width="1" stroke-dasharray="3 2" />
        <circle cx={hover.x} cy={hover.y} r="3"
                fill={hover.pnl >= 0 ? '#4ade80' : '#f87171'}
                stroke="#fff" stroke-width="1" />
        {@const _tx = Math.min(W - 110 - PAD_R, Math.max(PAD_L, hover.x + 8))}
        {@const _ty = Math.max(PAD_T + 4, hover.y - 30)}
        <rect x={_tx} y={_ty} width="110" height="26" rx="3"
              fill="#1d2a44" stroke="rgba(251,191,36,0.4)" stroke-width="1" />
        <text x={_tx + 6} y={_ty + 12}
              fill={hover.pnl >= 0 ? '#4ade80' : '#f87171'}
              font-size="10" font-weight="800" font-family="monospace">
          {hover.pnl >= 0 ? '+' : ''}₹{priceFmt(hover.pnl)}
        </text>
        <text x={_tx + 6} y={_ty + 22} fill="#c8d8f0"
              font-size="9" font-family="monospace">
          {hover.ts.slice(11, 19)}
        </text>
      {/if}
    </svg>
  {/if}
</div>

<style>
  .eq-shell {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-left: 3px solid #fbbf24;
    border-radius: 4px;
    padding: 8px 12px;
    width: 100%;
    max-width: 960px;
    box-sizing: border-box;
  }
  .eq-header-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
  }
  .eq-header {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #c8d8f0;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    flex: 1 1 auto;
  }
  .eq-final {
    margin-left: auto;
    font-family: ui-monospace, monospace;
    font-size: 0.78rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .eq-final-up   { color: #4ade80; }
  .eq-final-down { color: #f87171; }
  .eq-final-flat { color: #c8d8f0; }
  .eq-svg {
    width: 100%;
    display: block;
    cursor: crosshair;
  }
  .eq-empty {
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    text-align: center;
    padding: 1.2rem 0;
  }
</style>
