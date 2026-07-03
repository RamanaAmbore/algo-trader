<script>
  // Compact SVG line chart for price history during sim / paper / live.
  // Polls /api/charts/price-history and renders LTP as a line with order
  // event markers (placed / filled / unfilled). No chart library — the
  // chart panel is small enough that hand-rolled SVG is simpler and ships
  // zero JS bytes beyond what's already on the page.

  import { onDestroy, onMount } from 'svelte';
  import { fetchChartPriceHistory } from '$lib/api';
  import { priceFmt } from '$lib/format';
  import { visibleInterval } from '$lib/stores';
  import LegLabel from '$lib/LegLabel.svelte';
  import { createChartRefreshPulse } from '$lib/data/chartRefreshPulse.svelte.js';

  const _pulse = createChartRefreshPulse();

  let {
    /** @type {'sim'|'paper'|'live'} */ mode,
    /** @type {string} */ symbol,
    /** @type {number} */ height = 180,
    /** @type {number} */ pollMs = 3000,
    /** @type {boolean} */ autoPoll = true,
    // Optional pre-fetched data — when the parent does a batched
    // /charts/batch poll and distributes results, it passes the
    // ChartResponse for this symbol through here. The component then
    // skips its own polling. Pass `chartsBySymbol` for the underlying
    // overlay lookup so we don't re-hit the API.
    /** @type {any} */ data = null,
    /** @type {Record<string, any>} */ chartsBySymbol = null,
    // Replay-scrubber anchor — when the parent sets this to a
    // timestamp from the captured history, the chart pins a vertical
    // amber dashed line at that x so multiple charts on the page
    // share the same "you are here" visual reference.
    /** @type {string | null} */ scrubbedTs = null,
    // Yesterday's close. When set, drawn as a horizontal dashed line
    // across the chart with a small label at the y-axis. Universal
    // convention across Bloomberg / TWS / Kite — gives the operator
    // an instant "above prev close vs below" anchor without comparing
    // numbers. Parent supplies the value because the row that opens
    // the chart already has close_price from /api/positions.
    /** @type {number | null} */ prevClose = null,
  } = $props();

  /** @type {Array<{ts:string,ltp:number,bid:number|null,ask:number|null}>} */
  let ticks = $state([]);
  /** @type {Array<{ts:string,kind:string,side:string,price:number|null,status:string,order_id:number,attempts:number,detail:string|null}>} */
  let events = $state([]);
  // Classification surfaced by the API so the chart can render with the
  // right palette + label (underlyings get a sky-blue line, derivatives
  // get the amber LTP + lifecycle markers).
  /** @type {'underlying'|'derivative'|'other'} */
  let kind = $state(/** @type {any} */ ('other'));
  /** @type {string|null} */
  let underlying = $state(null);
  // Underlying overlay — when set, fetched alongside the primary ticks
  // and rendered as a faint sky line scaled to the option's y-range so
  // operators can see "spot −3% → call −40%" at a glance.
  /** @type {Array<{ts:string,ltp:number}>} */
  let underlyingTicks = $state([]);
  let error = $state('');
  let loading = $state(true);
  /** @type {(() => void) | null} */
  let timer = $state(null);
  let mounted = $state(true);
  /** @type {{x:number,y:number,kind:string,side:string,price:number|null,ts:string,detail:string|null,order_id:number,qty:number|null,slippage:number|null}|null} */
  let hover = $state(null);

  // True when the parent is feeding pre-fetched data; we skip our own
  // polling in that case so a page with N charts only does one round-trip
  // per refresh instead of N + N (option + underlying overlay).
  const externalData = $derived(data != null);

  function applyData(/** @type {any} */ r) {
    ticks      = r?.ticks  || [];
    events     = r?.events || [];
    kind       = r?.kind   || 'other';
    underlying = r?.underlying || null;
    error      = '';
    loading    = false;
    if (ticks.length) _pulse.notify('chart');
  }

  async function load() {
    if (!mode || !symbol) { loading = false; return; }
    if (externalData) {
      applyData(data);
      // Underlying overlay — read from the parent's batch response
      // (chartsBySymbol[underlying]) instead of issuing a fresh fetch.
      if (kind === 'derivative' && underlying && chartsBySymbol?.[underlying]) {
        const u = chartsBySymbol[underlying];
        underlyingTicks = (u.ticks || []).map(/** @param {any} t */ (t) => ({ ts: t.ts, ltp: t.ltp }));
      } else {
        underlyingTicks = [];
      }
      return;
    }
    try {
      const r = await fetchChartPriceHistory(mode, symbol);
      if (!mounted) return;
      applyData(r);
      // Fetch the underlying spot history when this is a derivative so the
      // chart can overlay it. Errors here are silent — the option chart
      // still renders without the overlay.
      if (kind === 'derivative' && underlying) {
        try {
          const u = await fetchChartPriceHistory(mode, underlying);
          underlyingTicks = (u?.ticks || []).map(/** @param {any} t */ (t) => ({ ts: t.ts, ltp: t.ltp }));
        } catch (_) { underlyingTicks = []; }
      } else {
        underlyingTicks = [];
      }
    } catch (e) {
      error = /** @type {any} */ (e).message || String(e);
      loading = false;
    }
  }

  function startPolling() {
    // Skip polling when the parent feeds data — its own poll cadence
    // will re-render us via the `data` prop changing.
    if (externalData || !autoPoll || !pollMs) return;
    stopPolling();
    // visibleInterval: pauses when hidden, fires load() immediately on
    // tab return so the chart refreshes as soon as the operator returns.
    timer = visibleInterval(load, pollMs);
  }
  function stopPolling() {
    if (timer) { timer(); timer = null; }
  }

  onMount(() => { load(); startPolling(); });
  onDestroy(() => { mounted = false; stopPolling(); });

  // Reload when props change. Earlier this pre-cleared ticks/events
  // before kicking off load(); if the new symbol's fetch failed (or
  // the symbol no longer existed), the chart stayed permanently blank.
  // Now we keep the prior chart visible until applyData() lands on
  // a successful response — `loading` flips on so a small "loading"
  // chip can render above the stale chart if a parent wants it.
  $effect(() => {
    void mode; void symbol; void data;
    if (!externalData) {
      loading = true; error = '';
    }
    load();
  });

  // Kill the self-poll timer the moment a parent starts feeding `data`,
  // so a page that flips from per-chart polling to batched feeds doesn't
  // accumulate a stale interval. Equally, restart polling if `data` ever
  // goes back to null (e.g. parent's batch endpoint failed permanently).
  $effect(() => {
    if (externalData) stopPolling();
    else if (autoPoll && pollMs && !timer) startPolling();
  });

  // ── Chart geometry ─────────────────────────────────────────────────
  const W = 720;            // viewBox width (scales to container via 100%)
  const PAD_L = 40, PAD_R = 8, PAD_T = 8, PAD_B = 22;

  const xAxisY = $derived(height - PAD_B);
  const innerW = $derived(W - PAD_L - PAD_R);
  const innerH = $derived(height - PAD_T - PAD_B);

  // ── Zoom + pan ─────────────────────────────────────────────────────
  // `zoom` overrides the auto x-domain when non-null. Set by wheel /
  // pinch / drag-pan, reset by the toolbar Reset button. Operator can
  // zoom into one minute of an order's chase to inspect the bid/ask
  // crawl, then pop back out with one click.
  /** @type {{xMin: number, xMax: number} | null} */
  let zoom = $state(null);
  /** @type {{startClientX: number, startMin: number, startMax: number} | null} */
  let pan = $state(null);

  // Time domain — `zoom` wins when set; otherwise span the full tick
  // history. Wheel zoom around the cursor narrows / widens this range
  // without re-fetching anything.
  const dataTMin = $derived(ticks.length ? +new Date(ticks[0].ts) : 0);
  const dataTMax = $derived(ticks.length ? +new Date(ticks[ticks.length - 1].ts) : 1);
  const tMin  = $derived(zoom ? zoom.xMin : dataTMin);
  const tMax  = $derived(zoom ? zoom.xMax : dataTMax);
  const tSpan = $derived(Math.max(1, tMax - tMin));
  const isZoomed = $derived(zoom !== null);

  // Price domain — auto-fits to the *visible* x-range. When the
  // operator zooms in on a 2-minute window during a chase, the y-axis
  // tightens to the bid/ask the chase actually saw, so the wiggle uses
  // the full vertical space instead of being squashed against the
  // pre-zoom min/max. Pads ±5% so the line doesn't kiss the frame.
  const visibleTicks = $derived(
    ticks.filter(t => {
      const ts = +new Date(t.ts);
      return ts >= tMin && ts <= tMax;
    })
  );
  const prices = $derived(
    (visibleTicks.length ? visibleTicks : ticks)
      .flatMap(t => [t.ltp, t.bid, t.ask].filter(v => v != null))
  );
  const pMin = $derived(prices.length ? Math.min(...prices) : 0);
  const pMax = $derived(prices.length ? Math.max(...prices) : 1);
  const pPad = $derived(Math.max((pMax - pMin) * 0.05, pMin * 0.0005, 0.5));
  const yMin = $derived(pMin - pPad);
  const yMax = $derived(pMax + pPad);
  const ySpan = $derived(Math.max(0.001, yMax - yMin));

  function xOf(/** @type {string} */ ts) {
    return PAD_L + ((+new Date(ts) - tMin) / tSpan) * innerW;
  }
  function yOf(/** @type {number} */ price) {
    return PAD_T + (1 - (price - yMin) / ySpan) * innerH;
  }

  // Path for the LTP line — use only visible ticks when zoomed so
  // path-build work is O(visible) not O(all 600) on every zoom change.
  const ltpPath = $derived.by(() => {
    const src = visibleTicks.length ? visibleTicks : ticks;
    if (!src.length) return '';
    return src.map((t, i) =>
      `${i === 0 ? 'M' : 'L'}${xOf(t.ts).toFixed(1)},${yOf(t.ltp).toFixed(1)}`
    ).join(' ');
  });

  // Underlying overlay path — rescaled into the option's y-range so the
  // shape of the spot move is visible alongside the option's price even
  // though the absolute values are wildly different (e.g. 22,000 vs 180).
  // Only drawn for derivative charts that received underlying ticks.
  const underlyingDomain = $derived.by(() => {
    if (!underlyingTicks.length) return null;
    let lo = Infinity, hi = -Infinity;
    for (const t of underlyingTicks) {
      if (t.ltp < lo) lo = t.ltp;
      if (t.ltp > hi) hi = t.ltp;
    }
    return { lo, hi, span: Math.max(0.001, hi - lo) };
  });
  const underlyingPath = $derived.by(() => {
    if (!underlyingTicks.length || !underlyingDomain || !ticks.length) return '';
    const { lo, span } = underlyingDomain;
    // Map the underlying's normalized 0..1 onto the option's plot area
    // (top = 1, bottom = 0). Use the option's plot extents so the line
    // rides through the middle of the chart, never clipping the frame.
    const top = PAD_T + 0.10 * innerH;
    const bot = PAD_T + 0.90 * innerH;
    // Filter to visible time range when zoomed — same 30× reduction as ltpPath.
    const src = visibleTicks.length
      ? underlyingTicks.filter(t => { const ms = +new Date(t.ts); return ms >= tMin && ms <= tMax; })
      : underlyingTicks;
    return (src.length ? src : underlyingTicks).map((t, i) => {
      const norm = (t.ltp - lo) / span;
      const y    = bot - norm * (bot - top);
      return `${i === 0 ? 'M' : 'L'}${xOf(t.ts).toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  });

  // Bid/ask shaded band (faint cyan area between bid and ask paths) —
  // only drawn when both sides are populated, so live/paper mode shows
  // the spread band but pure-LTP-only ticks (rare) skip it cleanly.
  // Uses visible ticks when zoomed for the same 30× speedup as ltpPath.
  const bandPath = $derived.by(() => {
    const src = visibleTicks.length ? visibleTicks : ticks;
    if (!src.length) return '';
    const top = src.filter(t => t.ask != null);
    const bot = src.filter(t => t.bid != null);
    if (!top.length || !bot.length) return '';
    const up = top.map(t => `${xOf(t.ts).toFixed(1)},${yOf(/**@type{number}*/(t.ask)).toFixed(1)}`);
    const dn = bot.slice().reverse().map(t => `${xOf(t.ts).toFixed(1)},${yOf(/**@type{number}*/(t.bid)).toFixed(1)}`);
    return `M${up.join(' L')} L${dn.join(' L')} Z`;
  });

  // Event markers — one circle per AlgoOrder lifecycle transition.
  const markerColors = /** @type {Record<string,string>} */ ({
    placed:   'var(--c-action)',  // amber
    filled:   'var(--c-long)',  // emerald
    unfilled: 'var(--c-short)',  // red
    chased:   '#7dd3fc',  // sky
  });

  // ── Zoom + pan handlers ───────────────────────────────────────────

  /** Map a client X (px) to a value in the current x-domain. */
  function _xValueAt(/** @type {SVGSVGElement} */ svg,
                     /** @type {number} */ clientX) {
    const rect = svg.getBoundingClientRect();
    const xPx  = (clientX - rect.left) * (W / rect.width);
    return tMin + ((xPx - PAD_L) / innerW) * tSpan;
  }

  function onWheel(/** @type {WheelEvent} */ e) {
    if (!ticks.length) return;
    e.preventDefault();
    const svg    = /** @type {SVGSVGElement} */ (e.currentTarget);
    const xVal   = _xValueAt(svg, e.clientX);
    const factor = e.deltaY > 0 ? 1.25 : 1 / 1.25;     // out / in
    let newMin = xVal - (xVal - tMin) * factor;
    let newMax = xVal + (tMax - xVal) * factor;
    // Clamp: never zoom out past the full data range.
    if (newMin <= dataTMin && newMax >= dataTMax) {
      zoom = null;
      return;
    }
    // Lower bound on width — 1 second — so users can't zoom into a
    // zero-width window and freeze the chart.
    if (newMax - newMin < 1000) return;
    zoom = { xMin: Math.max(newMin, dataTMin - tSpan), xMax: Math.min(newMax, dataTMax + tSpan) };
  }

  function onPointerDown(/** @type {PointerEvent} */ e) {
    if (!ticks.length || e.button !== 0) return;
    /** @type {any} */ const tgt = e.currentTarget;
    tgt.setPointerCapture?.(e.pointerId);
    pan = { startClientX: e.clientX, startMin: tMin, startMax: tMax };
  }
  function onPointerUp(/** @type {PointerEvent} */ e) {
    if (pan) {
      /** @type {any} */ const tgt = e.currentTarget;
      tgt.releasePointerCapture?.(e.pointerId);
    }
    pan = null;
  }
  function onPointerMoveSvg(/** @type {PointerEvent} */ e) {
    if (pan) {
      const rect = /** @type {SVGSVGElement} */ (e.currentTarget).getBoundingClientRect();
      const dxPx = (e.clientX - pan.startClientX) * (W / rect.width);
      const dxVal = (dxPx / innerW) * (pan.startMax - pan.startMin);
      zoom = { xMin: pan.startMin - dxVal, xMax: pan.startMax - dxVal };
      hover = null;   // suppress hover while dragging
    }
  }

  function resetZoom() { zoom = null; pan = null; }

  function showHover(/** @type {any} */ e) {
    hover = {
      x: xOf(e.ts), y: yOf(e.price ?? ticks[ticks.length - 1]?.ltp ?? 0),
      kind: e.kind, side: e.side, price: e.price, ts: e.ts,
      detail: e.detail, order_id: e.order_id,
      qty: e.qty, slippage: e.slippage,
    };
  }
  function hideHover() { hover = null; pinnedHover = false; }
  // Click-to-pin — operator: "make this default action for all the
  // charts". Click on the chart picks the nearest tick to the click
  // x-coordinate, snaps a popup carrying the timestamp + ltp + bid/ask
  // and pins it until the operator clicks again (or hits the × close
  // button on the popup).
  let pinnedHover = $state(false);
  function _onChartClick(/** @type {MouseEvent} */ e) {
    if (!ticks.length || pan) return;
    if (pinnedHover) { hideHover(); return; }
    const svg = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const xPx = (e.clientX - rect.left) * (W / rect.width);
    const xVal = tMin + ((xPx - PAD_L) / innerW) * (tMax - tMin);
    let best = ticks[0], bestD = Infinity;
    for (const t of ticks) {
      const d = Math.abs(+new Date(t.ts) - xVal);
      if (d < bestD) { bestD = d; best = t; }
    }
    hover = {
      x: xOf(best.ts), y: yOf(best.ltp ?? 0),
      kind: 'tick', side: '', price: best.ltp, ts: best.ts,
      detail: null, order_id: 0,
      qty: null, slippage: null,
    };
    pinnedHover = true;
  }

  function fmtPrice(/** @type {number|null} */ v) {
    if (v == null) return '—';
    return `₹${priceFmt(v)}`;
  }
  function fmtTime(/** @type {string} */ ts) {
    // IST-only HH:MM:SS for the hover tooltip — dual-TZ would crowd the
    // narrow tooltip box. Suffix marks the zone explicitly so the user
    // doesn't have to guess (chart tooltips are trading-critical: a
    // 14:32:48 reading means nothing without a zone tag).
    try {
      return new Date(ts).toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hour12: false, timeZone: 'Asia/Kolkata',
      }) + ' IST';
    } catch { return ts; }
  }

  // Y-axis labels — 5 evenly spaced (was 3, but the chart was sparse;
  // 5 lines reads as a proper grid without visual noise).
  const yTicks = $derived.by(() => {
    if (!prices.length) return [];
    const n = 5;
    return Array.from({ length: n }, (_, i) => {
      const v = yMin + (ySpan * i) / (n - 1);
      return { v, y: yOf(v) };
    });
  });

  // X-axis grid + labels — 4 evenly spaced verticals across the visible
  // time range. Each label shows HH:MM:SS so the operator can read off
  // when a particular price excursion happened.
  const xTicks = $derived.by(() => {
    if (!ticks.length) return [];
    const n = 4;
    return Array.from({ length: n }, (_, i) => {
      const t = tMin + (tSpan * i) / (n - 1);
      const date = new Date(t);
      const hh = String(date.getHours()).padStart(2, '0');
      const mm = String(date.getMinutes()).padStart(2, '0');
      const ss = String(date.getSeconds()).padStart(2, '0');
      return { t, x: PAD_L + ((t - tMin) / tSpan) * innerW, label: `${hh}:${mm}:${ss}` };
    });
  });
</script>

<div class="price-chart {_pulse.classOf('chart')}" style="--chart-h: {height}px">
  <div class="chart-header">
    <span class="chart-symbol">{symbol || '—'}</span>
    {#if kind === 'underlying'}
      <span class="chart-tag chart-tag-underlying">SPOT</span>
    {:else if kind === 'derivative'}
      <span class="chart-tag chart-tag-deriv">F&O</span>
    {/if}
    <span class="chart-mode chart-mode-{mode}">{mode?.toUpperCase()}</span>
    {#if underlyingTicks.length}
      <span class="chart-legend" title="Spot price of {underlying}, normalized to this chart's range">
        <span class="legend-dash" aria-hidden="true"></span>
        {underlying}
      </span>
    {/if}
    {#if loading}
      <span class="chart-status">loading…</span>
    {:else if error}
      <span class="chart-status chart-error" title={error}>error</span>
    {:else}
      <span class="chart-status">{ticks.length} ticks · {events.length} events</span>
    {/if}
    {#if isZoomed}
      <button type="button" class="chart-reset"
              title="Reset zoom — show the full tick history"
              onclick={resetZoom}>reset</button>
    {/if}
  </div>

  {#if !loading && !ticks.length}
    <div class="chart-empty">
      No price ticks captured yet for <span class="font-mono"><LegLabel sym={symbol} /></span>.
      Ticks are recorded once an order is open against the symbol{mode === 'sim' ? ' or the simulator is running' : ''}.
    </div>
  {:else if ticks.length}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <!-- Chart SVG: wheel-zoom + drag-pan + click-pin are pointer-native
         interactions. role="application" communicates this to AT; a
         keyboard-only zoom/pan equivalent is not practical for this chart type. -->
    <svg viewBox="0 0 {W} {height}" preserveAspectRatio="none"
         class="chart-svg" class:chart-panning={pan !== null}
         role="application" aria-label="Price chart — wheel to zoom, drag to pan, click to pin"
         onwheel={onWheel}
         onpointerdown={onPointerDown}
         onpointerup={onPointerUp}
         onpointermove={onPointerMoveSvg}
         onclick={_onChartClick}>
      <!-- Plot-area background tint — sits behind all other SVG children.
           Colour defined via --chart-bg-tint in app.css so all chart
           surfaces share the same subtle cyan fill. -->
      <rect class="chart-bg" x={PAD_L} y={PAD_T} width={innerW} height={innerH}
            fill="var(--chart-bg-tint)" rx="0"/>
      <!-- Y-axis grid + labels -->
      {#each yTicks as t}
        <line class="chart-grid-line" x1={PAD_L} x2={W - PAD_R} y1={t.y} y2={t.y}/>
        <text x={PAD_L - 4} y={t.y + 3} text-anchor="end"
              fill="#c8d8f0" font-size="11" font-weight="600" font-family="monospace">
          {priceFmt(t.v)}
        </text>
      {/each}
      <!-- X-axis grid + labels — verticals at 4 evenly spaced times so
           the operator can correlate price moves with wall-clock seconds
           without the hover crosshair. Skip the leftmost vertical (it
           overlaps the Y-axis already drawn by the labels). -->
      {#each xTicks as xt, i}
        {#if i > 0}
          <line class="chart-grid-line-minor" x1={xt.x} x2={xt.x} y1={PAD_T} y2={xAxisY}/>
        {/if}
        <text x={xt.x} y={height - 6}
              text-anchor={i === 0 ? 'start' : (i === xTicks.length - 1 ? 'end' : 'middle')}
              fill="#c8d8f0" font-size="11" font-weight="600" font-family="monospace">
          {xt.label}
        </text>
      {/each}
      <!-- X-axis baseline -->
      <line x1={PAD_L} x2={W - PAD_R} y1={xAxisY} y2={xAxisY}
            stroke="rgba(255,255,255,0.18)" stroke-width="1"/>

      <!-- Bid/ask band -->
      {#if bandPath}
        <path d={bandPath} fill="rgba(125,211,252,0.10)" stroke="none" class="data-path"/>
      {/if}

      <!-- Previous close reference line — dashed amber across the chart
           at yesterday's close so the operator reads "above prev / below
           prev" without comparing numbers. Bloomberg / TWS / Kite all
           ship this. Renders only when (a) prev close was supplied and
           (b) it sits inside the visible y-range. -->
      {#if prevClose != null && Number.isFinite(prevClose) && prevClose > 0}
        {@const _pcY = yOf(prevClose)}
        {#if _pcY >= PAD_T && _pcY <= xAxisY}
          <line x1={PAD_L} x2={W - PAD_R} y1={_pcY} y2={_pcY}
                stroke="rgba(251,191,36,0.55)" stroke-width="1"
                stroke-dasharray="4 3"/>
          <text x={W - PAD_R - 4} y={_pcY - 3}
                text-anchor="end" font-size="9"
                fill="rgba(251,191,36,0.85)" style="font-family: var(--font-numeric)">
            prev {prevClose.toFixed(2)}
          </text>
        {/if}
      {/if}

      <!-- Underlying overlay — sky-blue dashed line, normalized into the
           option's plot area. Operators see the spot move alongside the
           derived price without the option line getting squashed. -->
      {#if underlyingPath}
        <path d={underlyingPath} fill="none"
              stroke="#7dd3fc" stroke-width="1" stroke-dasharray="3 3"
              stroke-opacity="0.7" class="data-path"/>
      {/if}

      <!-- LTP line — sky-blue for underlyings (so it matches the index
           palette used elsewhere) and amber for derivatives / equities. -->
      <path d={ltpPath} fill="none"
            stroke={kind === 'underlying' ? '#7dd3fc' : 'var(--c-action)'}
            stroke-width="1.5" class="data-path"/>

      <!-- Order event markers -->
      {#each events as ev}
        {#if ev.ts >= ticks[0].ts && ev.ts <= ticks[ticks.length - 1].ts}
          {@const cx = xOf(ev.ts)}
          {@const cy = yOf(ev.price ?? ticks[ticks.length - 1].ltp)}
          <g class="ev-marker"
             onmouseenter={() => showHover(ev)}
             onmouseleave={hideHover}
             role="img" aria-label="{ev.kind} {ev.side}">
            <circle cx={cx} cy={cy} r="6"
                    fill={markerColors[ev.kind] || '#fff'}
                    fill-opacity="0.18"
                    stroke={markerColors[ev.kind] || '#fff'}
                    stroke-width="1.5"/>
            <circle cx={cx} cy={cy} r="2.5"
                    fill={markerColors[ev.kind] || '#fff'}/>
          </g>
        {/if}
      {/each}

      <!-- Replay-scrubber anchor (shared across all Lab charts via
           scrubbedTs prop). Vertical amber dashed line at the
           scrubbed timestamp. Suppressed while the operator is
           hovering on an event marker so the hover tooltip stays
           clean. -->
      {#if scrubbedTs && !hover}
        {@const _stMs = +new Date(scrubbedTs)}
        {#if Number.isFinite(_stMs) && _stMs >= tMin && _stMs <= tMax}
          <line x1={xOf(scrubbedTs)} x2={xOf(scrubbedTs)}
                y1={PAD_T} y2={height - PAD_B}
                stroke="rgba(251,191,36,0.7)" stroke-width="1.25"
                stroke-dasharray="4 3" />
        {/if}
      {/if}

      <!-- Hover tooltip — line 1: kind · side · qty; line 2: price ×
           qty = total @ time; line 3: order #N · slippage (if any). -->
      {#if hover}
        {@const _qty   = hover?.qty}
        {@const _px    = hover?.price}
        {@const _total = (_qty != null && _px != null) ? _qty * _px : null}
        {@const _slip  = hover?.slippage}
        {@const _h     = (_slip != null && _slip !== 0) ? 70 : 56}
        {@const tx = Math.min(W - 200 - PAD_R, Math.max(PAD_L, (hover?.x ?? 0) + 8))}
        {@const ty = Math.max(PAD_T, (hover?.y ?? 0) - _h - 4)}
        <g pointer-events="none">
          <rect x={tx} y={ty} width="200" height={_h} rx="4"
                fill="#1d2a44" stroke="rgba(251,191,36,0.4)" stroke-width="1"/>
          <text x={tx + 6} y={ty + 14} fill="#fbbf24"
                font-size="10" font-weight="700" font-family="monospace">
            {hover?.kind?.toUpperCase()} · {hover?.side}{#if _qty != null} · {_qty}×{/if}
          </text>
          <text x={tx + 6} y={ty + 28} fill="#c8d8f0"
                font-size="9" font-family="monospace">
            {fmtPrice(_px)}{#if _total != null} → {fmtPrice(_total)}{/if} @ {fmtTime(hover?.ts)}
          </text>
          <text x={tx + 6} y={ty + 42} fill="#7e97b8"
                font-size="9" font-family="monospace">
            order #{hover?.order_id}
          </text>
          {#if _slip != null && _slip !== 0}
            <text x={tx + 6} y={ty + 56}
                  fill={_slip < 0 ? 'var(--c-long)' : 'var(--c-short)'}
                  font-size="9" font-family="monospace">
              slippage {_slip < 0 ? '−' : '+'}{fmtPrice(Math.abs(_slip))}
            </text>
          {/if}
        </g>
      {/if}
    </svg>
  {/if}
</div>

<style>
  .price-chart {
    background: var(--card-bg-gradient);
    border: 1px solid rgba(251,191,36,0.18);
    border-left: 3px solid var(--c-action);
    border-radius: 4px;
    padding: 8px 12px 6px;
    width: 100%;
    /* Cap chart width on desktop so the viewBox (720×height) doesn't
       get horizontally stretched by preserveAspectRatio="none" into a
       distorted, low-density line. On mobile we want full-width.
       960px lets two charts sit side-by-side in a grid layout at
       1920+ widths and matches the natural aspect of a 720-unit
       viewBox at ~180px tall. */
    max-width: 960px;
    box-sizing: border-box;
  }
  .chart-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 4px;
    font-size: var(--fs-sm);
  }
  .chart-symbol {
    font-family: monospace;
    color: #7dd3fc;
    font-weight: 700;
  }
  .chart-mode {
    font-family: monospace;
    font-size: var(--fs-xs);
    padding: 1px 5px;
    border-radius: 3px;
    font-weight: 700;
    border: 1px solid currentColor;
  }
  .chart-mode-sim   { color: var(--c-action); }
  .chart-mode-paper { color: #7dd3fc; }
  .chart-mode-live  { color: var(--c-long); }
  /* Kind tag — distinguishes spot vs F&O at a glance, complementary to
     the mode tag. Subtler than the mode pill so it doesn't dominate. */
  .chart-tag {
    font-family: monospace;
    font-size: var(--fs-2xs);
    padding: 1px 4px;
    border-radius: 2px;
    font-weight: 700;
    letter-spacing: 0.04em;
    border: 1px solid rgba(255,255,255,0.15);
  }
  .chart-tag-underlying {
    background: rgba(125,211,252,0.12);
    color: #7dd3fc;
    border-color: rgba(125,211,252,0.45);
  }
  .chart-tag-deriv {
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
    border-color: rgba(251,191,36,0.35);
  }
  /* Legend for the underlying overlay — tiny dashed sample + the
     underlying name, matching the dashed sky-blue line on the chart. */
  .chart-legend {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    font-family: monospace;
    font-size: var(--fs-xs);
    color: #7dd3fc;
    padding: 1px 4px;
    border-radius: 2px;
    border: 1px solid rgba(125,211,252,0.25);
    background: rgba(125,211,252,0.05);
  }
  .legend-dash {
    width: 14px;
    height: 0;
    border-top: 1px dashed #7dd3fc;
    opacity: 0.8;
  }
  .chart-status {
    color: var(--algo-muted);
    margin-left: auto;
    font-family: monospace;
  }
  .chart-error { color: var(--c-short); }
  .chart-reset {
    font-family: monospace;
    font-size: var(--fs-2xs);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
    cursor: pointer;
    margin-left: 0.3rem;
  }
  .chart-reset:hover {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.65);
  }
  /* Wheel-zoom + drag-pan affordances. The crosshair cursor signals
     "interactive" before the user notices the wheel works. */
  .chart-svg {
    cursor: crosshair;
    touch-action: pan-y;
  }
  .chart-svg.chart-panning {
    cursor: grabbing;
  }
  .chart-empty {
    height: var(--chart-h, 180px);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--algo-muted);
    font-size: var(--fs-sm);
    font-family: monospace;
    text-align: center;
    padding: 0 1rem;
  }
  .chart-svg {
    width: 100%;
    height: var(--chart-h, 180px);
    display: block;
  }
  /* Fullscreen card → chart fills the viewport. Same idiom as
     OptionsPayoff — parent card carries `.fs-card-on`, we take the
     viewport minus card-header + a comfortable pad. */
  :global(.fs-card-on) .chart-svg,
  :global(.fs-card-on) .chart-empty {
    height: calc(100vh - 10rem) !important;
    min-height: 320px;
  }
  @media (max-width: 600px) {
    :global(.fs-card-on) .chart-svg,
    :global(.fs-card-on) .chart-empty {
      height: calc(100vh - 8rem) !important;
    }
  }
  :global(.price-chart .ev-marker) { cursor: pointer; }
</style>
