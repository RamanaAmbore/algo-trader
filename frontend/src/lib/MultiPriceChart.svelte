<script>
  // MultiPriceChart — overlay N price series on one SVG with a shared
  // x-axis (timestamps) and a normalized y-axis (% change from each
  // series' first captured tick). Built for the Lab simulator's
  // per-leg view: a long-call at ₹50 and a short-strangle wing at
  // ₹2,000 would otherwise need separate charts because their raw
  // y-scales differ by 40×. Normalizing each series to its t=0 value
  // lets the operator compare trajectories directly in one frame.
  //
  // The single-series PriceChart remains the right tool for an
  // underlying spot or a single contract; this is purely the
  // multi-leg comparison surface.

  /** @type {{
   *   series:      Array<{symbol: string, color: string, side?: 'LONG'|'SHORT', account?: string,
   *                       ticks: Array<{ts: string, ltp: number}>}>,
   *   height?:     number,
   *   title?:      string,
   *   emptyMsg?:   string,
   *   scrubbedTs?: string | null,
   * }} */
  import { untrack } from 'svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { createChartRefreshPulse } from '$lib/data/chartRefreshPulse.svelte.js';
  const { series = [], height = 240, title = '',
          emptyMsg = 'No ticks captured yet for any leg.',
          scrubbedTs = null } = $props();

  // ── Chart-refresh pulse ───────────────────────────────────────────
  // Fires whenever normSeries gains new data so the Lab operator
  // gets a subtle cyan flash confirming a new tick batch landed.
  const _pulse = createChartRefreshPulse();

  // Y-axis scale toggle — linear (default) vs symmetric-log. Log
  // makes a +400 % long-call and a +5 % short-strangle both readable
  // on the same chart, which the linear axis can't (the small move
  // gets squashed against zero). Symmetric-log handles negative
  // values by mirroring the log curve below zero.
  let _yScale = $state(/** @type {'lin'|'log'} */ ('lin'));
  // Symlog inflection point (% above which we switch to log
  // compression). Below ±1 % we stay linear so noise near zero
  // doesn't blow up visually.
  const _symLogLinThresh = 0.01;
  function _symLog(/** @type {number} */ x) {
    if (_yScale === 'lin') return x;
    const sign = x < 0 ? -1 : 1;
    const ax = Math.abs(x);
    if (ax <= _symLogLinThresh) return x;
    return sign * (_symLogLinThresh + Math.log10(ax / _symLogLinThresh) * _symLogLinThresh);
  }

  // ── Chart geometry ─────────────────────────────────────────────────
  const W      = 720;
  const PAD_L  = 44;
  const PAD_R  = 16;
  const PAD_T  = 8;
  const PAD_B  = 28;
  const innerW = $derived(W - PAD_L - PAD_R);
  const innerH = $derived(height - PAD_T - PAD_B);

  // ── Build normalized series ────────────────────────────────────────
  // For each input series, derive {pctTicks: [{ts, pct, raw}]} where
  // pct = (ltp - base) / base, base = first tick's ltp. Series whose
  // first tick is 0 or missing are skipped (can't normalize against
  // zero). All series share the same time domain (the union of all
  // timestamps across all input series).
  const normSeries = $derived.by(() => {
    /** @type {Array<{symbol:string,color:string,side?:string,account?:string,
     *                pctTicks:Array<{ts:string,pct:number,raw:number}>,
     *                base:number}>} */
    const out = [];
    for (const s of series) {
      const t = s.ticks || [];
      if (!t.length) continue;
      // Use the first NON-ZERO tick as the % baseline. Symptom of the
      // earlier bug: pills updated (reading status.positions directly)
      // but the chart stayed blank because the very first captured
      // tick had ltp=0 (broker quote hadn't landed yet) — base=0 →
      // series dropped → all subsequent valid ticks discarded. Now we
      // walk past leading zeros and only drop a series when EVERY tick
      // is zero.
      let baseIdx = -1;
      for (let i = 0; i < t.length; i++) {
        if (Number(t[i].ltp) > 0) { baseIdx = i; break; }
      }
      if (baseIdx < 0) continue;
      const base = Number(t[baseIdx].ltp);
      // Keep all ticks from the first non-zero onward; trailing /
      // mid-series zeros are passed through (they'll plot as -100 %
      // dips, which is the truthful visual for "we lost the quote").
      const startedTicks = t.slice(baseIdx);
      out.push({
        symbol:  s.symbol,
        color:   s.color || '#7dd3fc',
        side:    s.side,
        account: s.account,
        base,
        pctTicks: startedTicks.map((tk) => ({
          ts:  tk.ts,
          pct: (Number(tk.ltp) - base) / base,
          raw: Number(tk.ltp),
        })),
      });
    }
    return out;
  });

  // Fire pulse when normSeries becomes non-empty (new tick batch landed
  // from the simulator or replay). Skips empty resets so the pulse is
  // only a positive signal ("data arrived").
  $effect(() => { if (normSeries.length) untrack(() => _pulse.notify('mpc')); });

  // Unified x-domain (min/max timestamp across every series). When
  // every captured tick collapses to the same timestamp (sub-second
  // sim with seconds-rounded ts before the backend fix, or only one
  // tick captured) we synthesise a 1-second window so the chart still
  // renders a flat line / single point instead of going blank.
  const xDomain = $derived.by(() => {
    let lo = Infinity, hi = -Infinity;
    for (const s of normSeries) {
      for (const tk of s.pctTicks) {
        const t = Date.parse(tk.ts);
        if (Number.isFinite(t)) {
          if (t < lo) lo = t;
          if (t > hi) hi = t;
        }
      }
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    if (hi === lo) return { lo: lo - 500, hi: hi + 500 };
    return { lo, hi };
  });

  // Unified y-domain (min/max pct across every series). Symmetric
  // around 0 so up moves and down moves are equally visible.
  const yDomain = $derived.by(() => {
    let mag = 0;
    for (const s of normSeries) {
      for (const tk of s.pctTicks) {
        const v = Math.abs(tk.pct);
        if (v > mag) mag = v;
      }
    }
    // Floor at 1 % so a flat market doesn't collapse to a degenerate axis.
    return Math.max(mag * 1.10, 0.01);
  });

  const xOf = (/** @type {number} */ tMs) => {
    if (!xDomain) return PAD_L;
    if (xDomain.hi === xDomain.lo) return PAD_L + innerW / 2;
    return PAD_L + ((tMs - xDomain.lo) / (xDomain.hi - xDomain.lo)) * innerW;
  };
  const yOf = (/** @type {number} */ pct) => {
    // pct in [-yDomain, +yDomain]. yOf(+yDomain) = top (PAD_T).
    // yOf(0) = vertical centre. yOf(-yDomain) = bottom (PAD_T+innerH).
    // _symLog is identity when scale=lin, otherwise symmetric-log.
    const v   = _symLog(pct);
    const dom = _symLog(yDomain);
    return PAD_T + innerH / 2 - (v / dom) * (innerH / 2);
  };

  function buildPath(/** @type {Array<{ts:string,pct:number}>} */ pts) {
    if (!pts.length) return '';
    let d = '';
    for (let i = 0; i < pts.length; i++) {
      const t = Date.parse(pts[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = xOf(t);
      const y = yOf(pts[i].pct);
      d += (i === 0 ? `M${x.toFixed(2)},${y.toFixed(2)}`
                    : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  }

  // Y-axis tick marks at ±yDomain, ±yDomain/2, 0.
  const yTicks = $derived([
    -yDomain, -yDomain / 2, 0, yDomain / 2, yDomain,
  ]);
  const pctFmt = (/** @type {number} */ v) => {
    const sign = v > 0 ? '+' : '';
    return `${sign}${(v * 100).toFixed(1)}%`;
  };

  // X-axis labels — 4 evenly-spaced timestamps.
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

  // Hover crosshair — closest tick by x in the union of all series.
  /** @type {{x:number,y:number,ts:string,rows:Array<{symbol:string,color:string,pct:number,raw:number}>}|null} */
  let hover = $state(null);

  function onPointerMove(/** @type {PointerEvent} */ ev) {
    if (!xDomain || !normSeries.length) { hover = null; return; }
    const svg = /** @type {SVGSVGElement} */ (ev.currentTarget);
    const rect = svg.getBoundingClientRect();
    const xRel = ((ev.clientX - rect.left) / rect.width) * W;
    const tMs = xDomain.lo + ((xRel - PAD_L) / innerW) * (xDomain.hi - xDomain.lo);
    // For each series, find the tick closest to tMs.
    const rows = normSeries.map((s) => {
      let best = s.pctTicks[0];
      let bestD = Infinity;
      for (const tk of s.pctTicks) {
        const d = Math.abs(Date.parse(tk.ts) - tMs);
        if (d < bestD) { bestD = d; best = tk; }
      }
      return { symbol: s.symbol, color: s.color, pct: best.pct, raw: best.raw, ts: best.ts };
    });
    // Anchor crosshair to the median tick's x.
    const anchorT = Date.parse(rows[0]?.ts || '') || tMs;
    hover = {
      x:    xOf(anchorT),
      y:    PAD_T + innerH / 2,
      ts:   rows[0]?.ts || '',
      rows: rows.map(({ symbol, color, pct, raw }) => ({ symbol, color, pct, raw })),
    };
  }
  function onPointerLeave() { hover = null; }
</script>

<div class="mpc-shell {_pulse.classOf('mpc')}">
  <div class="mpc-header-row">
    {#if title}
      <div class="mpc-header">{title}</div>
    {/if}
    <!-- Y-axis scale toggle. Linear default; symlog when a wide-range
         leg basket has both ±400 % and ±5 % moves on the same chart. -->
    <div class="mpc-yscale" role="group" aria-label="Y-axis scale">
      <button type="button" class="mpc-yscale-btn"
              class:on={_yScale === 'lin'}
              onclick={() => _yScale = 'lin'}
              title="Linear scale (default)">lin</button>
      <button type="button" class="mpc-yscale-btn"
              class:on={_yScale === 'log'}
              onclick={() => _yScale = 'log'}
              title="Symmetric-log scale — compresses big moves so small-magnitude legs stay readable.">log</button>
    </div>
  </div>

  {#if !normSeries.length}
    <div class="mpc-empty">{emptyMsg}</div>
  {:else}
    <svg viewBox="0 0 {W} {height}" preserveAspectRatio="none" class="mpc-svg"
         role="img" aria-label="Multi-leg premium chart"
         onpointermove={onPointerMove} onpointerleave={onPointerLeave}>
      <!-- Plot-area background tint — first child so it sits behind
           all grid lines, paths, and labels. --chart-bg-tint in app.css. -->
      <rect class="chart-bg" x={PAD_L} y={PAD_T} width={innerW} height={innerH}
            fill="var(--chart-bg-tint)" rx="0"/>
      <!-- y-axis grid lines + labels (pct) -->
      {#each yTicks as v}
        <line x1={PAD_L} x2={W - PAD_R} y1={yOf(v)} y2={yOf(v)}
              class={v === 0 ? 'chart-grid-zero' : 'chart-grid-line'} />
        <text x={PAD_L - 6} y={yOf(v) + 3} text-anchor="end"
              fill="#c8d8f0" font-size="11" font-weight="600">{pctFmt(v)}</text>
      {/each}

      <!-- x-axis labels -->
      {#each xLabels as l}
        <line class="chart-grid-line-minor" x1={l.x} x2={l.x} y1={PAD_T} y2={height - PAD_B} />
        <text x={l.x} y={height - PAD_B + 14} text-anchor="middle"
              fill="#c8d8f0" font-size="11" font-weight="600">{l.label}</text>
      {/each}

      <!-- One path per series -->
      {#each normSeries as s}
        <path d={buildPath(s.pctTicks)} fill="none" class="data-path"
              stroke={s.color} stroke-width="1.8"
              stroke-linejoin="round" stroke-linecap="round" />
      {/each}

      <!-- Replay-scrubber anchor (when external scrubbedTs prop is
           set, the chart pins a vertical amber line at that
           timestamp so all charts on the page share the same
           visual reference moment). Hover crosshair takes precedence
           when the operator moves the cursor over the chart. -->
      {#if scrubbedTs && !hover}
        {@const _stMs = Date.parse(scrubbedTs)}
        {#if Number.isFinite(_stMs)}
          <line x1={xOf(_stMs)} x2={xOf(_stMs)} y1={PAD_T} y2={height - PAD_B}
                stroke="rgba(251,191,36,0.7)" stroke-width="1.25"
                stroke-dasharray="4 3" />
        {/if}
      {/if}

      <!-- Hover crosshair -->
      {#if hover}
        <line x1={hover?.x} x2={hover?.x} y1={PAD_T} y2={height - PAD_B}
              stroke="rgba(251,191,36,0.6)" stroke-width="1" stroke-dasharray="3 2" />
      {/if}
    </svg>

    <!-- Legend -->
    <div class="mpc-legend">
      {#each normSeries as s}
        <span class="mpc-legend-row">
          <span class="mpc-swatch" style="background:{s.color}"></span>
          {#if s.side}<span class="mpc-side mpc-side-{s.side.toLowerCase()}">{s.side}</span>{/if}
          <span class="mpc-sym">{formatSymbol(s.symbol)}</span>
          {#if s.account}<span class="mpc-acct">{s.account}</span>{/if}
          {#if hover}
            {@const row = hover?.rows?.find((r) => r.symbol === s.symbol)}
            {#if row}
              <span class="mpc-val mpc-val-{row.pct >= 0 ? 'up' : 'down'}">
                {pctFmt(row.pct)}
              </span>
              <span class="mpc-raw">@₹{row.raw.toFixed(2)}</span>
            {/if}
          {/if}
        </span>
      {/each}
    </div>
  {/if}
</div>

<style>
  .mpc-shell {
    background: var(--card-bg-gradient);
    border: 1px solid rgba(251,191,36,0.18);
    border-left: 3px solid var(--c-action);
    border-radius: 4px;
    padding: 8px 12px;
    width: 100%;
    max-width: 960px;
    box-sizing: border-box;
  }
  .mpc-header-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
  }
  .mpc-header {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--algo-slate);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    flex: 1 1 auto;
  }
  .mpc-yscale {
    display: inline-flex;
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 3px;
    overflow: hidden;
    margin-left: auto;
  }
  .mpc-yscale-btn {
    padding: 0.15rem 0.45rem;
    border: none;
    background: transparent;
    color: var(--text-muted);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
  }
  .mpc-yscale-btn + .mpc-yscale-btn { border-left: 1px solid rgba(255,255,255,0.12); }
  .mpc-yscale-btn:hover { background: rgba(126,151,184,0.10); color: #f1f7ff; }
  .mpc-yscale-btn.on {
    background: rgba(74,222,128,0.18);
    color: var(--c-long);
  }
  .mpc-svg {
    width: 100%;
    display: block;
    cursor: crosshair;
  }
  .mpc-empty {
    color: var(--algo-muted);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    text-align: center;
    padding: 1.2rem 0;
  }
  .mpc-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.55rem;
    margin-top: 0.35rem;
    padding-top: 0.3rem;
    border-top: 1px solid rgba(200,216,240,0.08);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
  }
  .mpc-legend-row {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
  }
  .mpc-swatch {
    display: inline-block;
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 2px;
  }
  .mpc-side {
    font-weight: 800;
    letter-spacing: 0.04em;
    padding: 0 0.18rem;
  }
  .mpc-side-long  { color: #67e8f9; }
  .mpc-side-short { color: var(--c-action); }
  .mpc-sym {
    color: #f1f7ff;
    font-weight: 700;
  }
  .mpc-acct {
    color: var(--algo-muted);
  }
  .mpc-val {
    margin-left: 0.2rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .mpc-val-up   { color: var(--c-long); }
  .mpc-val-down { color: var(--c-short); }
  .mpc-raw {
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
  }
</style>
