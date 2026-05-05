<script>
  // Payoff diagram for a single-leg option position.
  //
  // X-axis: underlying spot, ranging ±span_pct around current spot.
  // Y-axis: position P&L in rupees (already net of entry cost).
  // Two curves:
  //   - Today's value (BS @ current DTE/IV) — amber, what the position
  //     would be worth if the underlying moves NOW.
  //   - Expiry value (intrinsic only) — sky-blue dashed, what it'd be
  //     worth at settlement.
  // Markers: current spot (cyan), strike (white), breakeven (magenta).
  // Profit zone shaded green, loss zone shaded red.

  /** @type {{
   *   payoff: Array<{spot:number,today_value:number,expiry_value:number}>,
   *   spot:         number,
   *   breakeven?:   number,
   *   breakevens?:  number[],
   *   intermediateCurves?: Array<{label:string,elapsed_pct:number,days_left:number,values:number[]}>,
   *   height?:      number,
   *   currentPnl?:  number|null,
   *   spanSigmas?:  number,
   *   spanPct?:     number,
   *   dte?:         number|null,
   *   ivProxy?:     number|null,
   *   legCount?:    number|null,
   *   realizedPnl?: number,
   *   onRefresh?:   (() => void) | null,
   *   loading?:     boolean,
   *   prevClose?:   number|null,
   * }} */
  let {
    payoff = [],
    spot,
    breakeven  = undefined,
    breakevens = /** @type {number[]|undefined} */ (undefined),
    // Time-slice curves between Today and Expiry. Each entry's
    // `values` is parallel-indexed to `payoff` (same spot grid).
    // Empty array → no slices (default; legacy single-leg mode
    // and any caller that doesn't opt-in via `time_slices` keeps
    // the original two-curve chart).
    intermediateCurves = /** @type {Array<{label:string,elapsed_pct:number,days_left:number,values:number[]}>} */ ([]),
    height     = 280,
    currentPnl = null,
    spanSigmas = 0,
    spanPct    = 0,
    dte        = /** @type {number|null|undefined} */ (null),
    ivProxy    = /** @type {number|null|undefined} */ (null),
    legCount   = /** @type {number|null|undefined} */ (null),
    // Realised P&L from positions closed today (qty=0). Surfaced as
    // REAL + TOTAL rows in the stat overlay so the chart's day-P&L
    // reconciles with the dashboard's per-underlying P&L:
    //   TDAY (open-leg theoretical) + REAL (closed-leg realised)
    //   ≈ dashboard ₹ for this underlying
    // 0 (default) → REAL + TOTAL rows hide; chart reads as before.
    realizedPnl = 0,
    onRefresh  = /** @type {(() => void) | null} */ (null),
    loading    = false,
    prevClose  = /** @type {number|null|undefined} */ (null),
  } = $props();

  // Day's direction — flag the SPOT readout green when trading above
  // yesterday's close, red below. Falls through to the neutral cyan
  // when prev_close isn't available (override / sim / fallback).
  const spotDir = $derived.by(() => {
    if (prevClose == null || prevClose <= 0) return 'flat';
    if (spot >  prevClose) return 'pos';
    if (spot <  prevClose) return 'neg';
    return 'flat';
  });

  // Apply the realised-P&L offset to the entire payoff curve. Closed
  // positions (qty=0) contribute realised P&L that doesn't move with
  // spot — a constant vertical shift on both today and expiry curves.
  // The shift makes the chart's TDAY at current spot match the
  // dashboard's per-underlying P&L (which is broker pnl total).
  // Without this, closed-out CRUDEOIL trades caused a permanent gap.
  // When realizedPnl=0, this is a no-op (returns the original array
  // by reference is not safe under Svelte 5; we rebuild defensively).
  const adjustedPayoff = $derived.by(() => {
    if (!payoff.length) return payoff;
    if (!realizedPnl || realizedPnl === 0) return payoff;
    return payoff.map(p => ({
      spot:         p.spot,
      today_value:  p.today_value  + realizedPnl,
      expiry_value: p.expiry_value + realizedPnl,
    }));
  });

  // Recompute breakevens from the SHIFTED expiry curve when there's
  // an offset — caller's `breakevens` prop came from the unshifted
  // backend curve, so its zero-crossings are off by `realizedPnl`
  // worth of vertical space when we plot the shifted curve.
  const adjustedBreakevens = $derived.by(() => {
    if (!realizedPnl || realizedPnl === 0) return null;
    if (!adjustedPayoff || adjustedPayoff.length < 2) return null;
    /** @type {number[]} */
    const bes = [];
    for (let i = 1; i < adjustedPayoff.length; i++) {
      const a = adjustedPayoff[i - 1];
      const b = adjustedPayoff[i];
      if ((a.expiry_value <= 0 && b.expiry_value >= 0)
       || (a.expiry_value >= 0 && b.expiry_value <= 0)) {
        const dy = b.expiry_value - a.expiry_value;
        if (dy === 0) continue;
        const t = -a.expiry_value / dy;
        bes.push(a.spot + t * (b.spot - a.spot));
      }
    }
    return bes;
  });

  // Nearest curve point to current spot — drives the on-chart TDAY/EXP
  // readouts so the operator sees position P&L right beside the chart.
  // Reads from the offset-adjusted curve so the overlay value equals
  // the dashboard's per-underlying P&L.
  const curveAtSpot = $derived.by(() => {
    const src = adjustedPayoff;
    if (!src.length) return null;
    let best = src[0];
    let bestDiff = Math.abs(best.spot - spot);
    for (const p of src) {
      const d = Math.abs(p.spot - spot);
      if (d < bestDiff) { bestDiff = d; best = p; }
    }
    return best;
  });

  // Multi-leg charts pass `breakevens` array; single-leg charts pass
  // a scalar. Normalise to an array so the render code is one path;
  // undefined / empty falls through to no markers.
  // strike / strikes props are kept on the API surface for back-
  // compat but no longer rendered (operator removed strike verticals
  // from the chart — see "spot/strike removal" below).
  // When the curve is shifted by realizedPnl, the backend-supplied
  // breakevens are stale (their zero-crossings were on the unshifted
  // curve). Use the locally-recomputed list instead.
  const breakevenList = $derived(adjustedBreakevens
    ? adjustedBreakevens.filter(b => b != null)
    : (breakevens
       ? breakevens.filter(b => b != null)
       : (breakeven != null ? [breakeven] : [])));

  /** @type {{x:number,y:number,spot:number,today:number,expiry:number}|null} */
  let hover = $state(null);

  // ── Geometry ──────────────────────────────────────────────────────
  const W = 720;
  // PAD_B widened from 28 → 40 so rotated σ labels (-30°) and the
  // breakeven labels stacked beneath them have the vertical room they
  // need without colliding with the chart legend.
  const PAD_L = 50, PAD_R = 12, PAD_T = 12, PAD_B = 40;
  const innerW = $derived(W - PAD_L - PAD_R);
  const innerH = $derived(height - PAD_T - PAD_B);

  // ── Zoom + pan state ──────────────────────────────────────────────
  // Operator can wheel-zoom into one strike or breakeven cluster, then
  // drag-pan to scan along the spot axis. Reset button (visible when
  // zoomed) snaps back to the auto ±2.5σ range supplied by the API.
  /** @type {{xMin: number, xMax: number} | null} */
  let zoom = $state(null);
  /** @type {{startClientX: number, startMin: number, startMax: number} | null} */
  let pan = $state(null);

  // X domain — `zoom` overrides the auto-derived spot range.
  const dataMin = $derived(payoff.length ? payoff[0].spot : (spot - 1));
  const dataMax = $derived(payoff.length ? payoff[payoff.length - 1].spot : (spot + 1));
  const sMin  = $derived(zoom ? zoom.xMin : dataMin);
  const sMax  = $derived(zoom ? zoom.xMax : dataMax);
  const sSpan = $derived(Math.max(0.001, sMax - sMin));
  const isZoomed = $derived(zoom !== null);

  // Y domain: union of both curves over the *visible* x-range. When
  // the operator zooms into a narrow spot range, the y-axis tightens
  // to the P&L excursion that's actually on screen — otherwise an
  // out-of-view +∞ wing of a long call would dominate the y-axis even
  // after zooming away from it. Force zero into the domain so the
  // loss/profit shading lands on the actual breakeven line.
  const visiblePayoff = $derived(
    adjustedPayoff.filter(p => p.spot >= sMin && p.spot <= sMax)
  );
  const yDomain = $derived.by(() => {
    const src = visiblePayoff.length ? visiblePayoff : adjustedPayoff;
    let lo = 0, hi = 0;
    for (const p of src) {
      if (p.today_value < lo)  lo = p.today_value;
      if (p.expiry_value < lo) lo = p.expiry_value;
      if (p.today_value > hi)  hi = p.today_value;
      if (p.expiry_value > hi) hi = p.expiry_value;
    }
    const pad = Math.max((hi - lo) * 0.10, 100);
    return { lo: lo - pad, hi: hi + pad, span: Math.max(1, (hi + pad) - (lo - pad)) };
  });

  function xOf(/** @type {number} */ s) {
    return PAD_L + ((s - sMin) / sSpan) * innerW;
  }
  function yOf(/** @type {number} */ v) {
    const { lo, span } = yDomain;
    return PAD_T + (1 - (v - lo) / span) * innerH;
  }
  // Y position of zero P&L line — the breakeven horizontal.
  const zeroY = $derived(yOf(0));

  // SVG paths — read from adjustedPayoff so the curve renders WITH
  // the realised-P&L offset applied. At realizedPnl=0 this equals
  // payoff (no-op pass-through).
  const pathToday = $derived.by(() => {
    if (!adjustedPayoff.length) return '';
    return adjustedPayoff.map((p, i) => `${i === 0 ? 'M' : 'L'}${xOf(p.spot).toFixed(1)},${yOf(p.today_value).toFixed(1)}`).join(' ');
  });
  const pathExpiry = $derived.by(() => {
    if (!adjustedPayoff.length) return '';
    return adjustedPayoff.map((p, i) => `${i === 0 ? 'M' : 'L'}${xOf(p.spot).toFixed(1)},${yOf(p.expiry_value).toFixed(1)}`).join(' ');
  });

  // Time-slice curves — one path per intermediate slice, parallel-
  // indexed against `payoff` (same spot grid). Stroke colour is
  // interpolated from amber (today) to sky-cyan (expiry) via HSL,
  // so the operator reads the family of curves as a smooth time
  // gradient. Dashed at the same cadence as the expiry curve so
  // they sit visually between today's solid and the dashed expiry
  // line. Thinner than the two anchor curves so they don't crowd.
  function _slerpAmberToSky(/** @type {number} */ t) {
    // Amber: hsl(43, 96%, 56%) — Tailwind amber-400 / `#fbbf24`
    // Sky:   hsl(199, 95%, 74%) — Tailwind sky-300 / `#7dd3fc`
    const h = 43  + (199 - 43)  * t;
    const s = 96  + (95  - 96)  * t;
    const l = 56  + (74  - 56)  * t;
    return `hsl(${h.toFixed(1)} ${s.toFixed(1)}% ${l.toFixed(1)}%)`;
  }
  const intermediatePaths = $derived.by(() => {
    if (!payoff.length || !intermediateCurves.length) return [];
    return intermediateCurves.map((c) => {
      const vals = c.values || [];
      // Defensive: only walk the part of the curve that has values
      // for. Mismatched lengths shouldn't happen (the backend builds
      // both arrays off the same spot grid) but it'd silently render
      // a broken path otherwise. Each `vals[i]` gets the same
      // realised offset as today + expiry curves so the slice
      // family stays vertically aligned with them.
      const n = Math.min(vals.length, payoff.length);
      let d = '';
      for (let i = 0; i < n; i++) {
        d += `${i === 0 ? 'M' : 'L'}${xOf(payoff[i].spot).toFixed(1)},${yOf(vals[i] + (realizedPnl || 0)).toFixed(1)} `;
      }
      return {
        label:    c.label,
        days:     c.days_left,
        elapsed:  c.elapsed_pct,
        d:        d.trim(),
        color:    _slerpAmberToSky(c.elapsed_pct ?? 0.5),
      };
    });
  });

  // Profit + loss zones — shade above and below zero on the today curve
  // up to the chart bounds. Two filled paths whose top/bottom rides the
  // today curve and whose other edge is the chart's boundary.
  const fillProfit = $derived.by(() => {
    if (!adjustedPayoff.length) return '';
    const top = adjustedPayoff.map(p => `${xOf(p.spot).toFixed(1)},${yOf(Math.max(0, p.today_value)).toFixed(1)}`);
    const lastX  = xOf(adjustedPayoff[adjustedPayoff.length - 1].spot).toFixed(1);
    const firstX = xOf(adjustedPayoff[0].spot).toFixed(1);
    return `M${firstX},${zeroY.toFixed(1)} L${top.join(' L')} L${lastX},${zeroY.toFixed(1)} Z`;
  });
  const fillLoss = $derived.by(() => {
    if (!adjustedPayoff.length) return '';
    const bot = adjustedPayoff.map(p => `${xOf(p.spot).toFixed(1)},${yOf(Math.min(0, p.today_value)).toFixed(1)}`);
    const lastX  = xOf(adjustedPayoff[adjustedPayoff.length - 1].spot).toFixed(1);
    const firstX = xOf(adjustedPayoff[0].spot).toFixed(1);
    return `M${firstX},${zeroY.toFixed(1)} L${bot.join(' L')} L${lastX},${zeroY.toFixed(1)} Z`;
  });

  function fmtMoney(/** @type {number} */ v) {
    const sign = v < 0 ? '-' : v > 0 ? '+' : '';
    return `${sign}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
  }
  function fmtSpot(/** @type {number} */ v) {
    return `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
  }

  // After dismiss, suppress hover for a short window so the cursor's
  // pointermove (still on the chart since the operator just clicked
  // there) doesn't immediately re-create the tooltip — the visible
  // glitch was "click sometimes works, sometimes not", actually a
  // dismiss-then-instant-rehover. 350 ms gives the operator time to
  // move the cursor away if they don't want the tooltip back; if
  // they DO want it back, any movement after the window re-arms.
  let _hoverSuppressUntil = 0;
  function _dismissHover() {
    hover = null;
    _hoverSuppressUntil = Date.now() + 350;
  }

  // Snap the tooltip to the payoff point nearest to the given client X.
  // Shared between hover (mouse pointermove) and tap (touch pointerdown)
  // so the touch path produces the same tooltip as desktop hover.
  function _setHoverFromClientX(/** @type {SVGSVGElement} */ svg,
                                /** @type {number} */ clientX) {
    if (Date.now() < _hoverSuppressUntil) return;
    // Hover tooltip reads values from adjustedPayoff so the
    // displayed TDAY / EXP at the hover spot already include the
    // realised offset — same as the on-chart curves they're
    // probing.
    const src = adjustedPayoff;
    if (!src.length) return;
    const rect = svg.getBoundingClientRect();
    const xPx  = (clientX - rect.left) * (W / rect.width);
    const xVal = sMin + ((xPx - PAD_L) / innerW) * sSpan;
    let best = src[0];
    let bestDiff = Math.abs(best.spot - xVal);
    for (const p of src) {
      const d = Math.abs(p.spot - xVal);
      if (d < bestDiff) { best = p; bestDiff = d; }
    }
    hover = {
      x: xOf(best.spot), y: yOf(best.today_value),
      spot: best.spot, today: best.today_value, expiry: best.expiry_value,
    };
  }

  function onPointerMove(/** @type {PointerEvent} */ e) {
    if (!payoff.length) return;
    const svg = /** @type {SVGSVGElement} */ (e.currentTarget);
    if (pan) {
      const rect = svg.getBoundingClientRect();
      const dxPx = (e.clientX - pan.startClientX) * (W / rect.width);
      const dxVal = (dxPx / innerW) * (pan.startMax - pan.startMin);
      zoom = { xMin: pan.startMin - dxVal, xMax: pan.startMax - dxVal };
      hover = null;
      return;
    }
    _setHoverFromClientX(svg, e.clientX);
  }
  // Mouse leave clears hover. Touch keeps it pinned until the operator
  // taps again (toggle behaviour, see onPointerDown). Plain
  // `hover = null` here (no _dismissHover) — leaving the chart isn't
  // an explicit "dismiss" gesture; re-entering should hover normally.
  function onPointerLeave(/** @type {PointerEvent} */ e) {
    if (e.pointerType !== 'touch') hover = null;
  }
  // Esc dismisses a pinned tooltip on desktop too — keyboard equivalent
  // of the touch-tap-to-toggle path. Listener mounts only while hover
  // is non-null so we don't sit on a global keydown for nothing.
  $effect(() => {
    if (!hover) return;
    const onKey = (/** @type {KeyboardEvent} */ e) => {
      if (e.key === 'Escape') _dismissHover();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  function onWheel(/** @type {WheelEvent} */ e) {
    if (!payoff.length) return;
    e.preventDefault();
    const svg  = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const xPx  = (e.clientX - rect.left) * (W / rect.width);
    const xVal = sMin + ((xPx - PAD_L) / innerW) * sSpan;
    const factor = e.deltaY > 0 ? 1.25 : 1 / 1.25;
    const newMin = xVal - (xVal - sMin) * factor;
    const newMax = xVal + (sMax - xVal) * factor;
    if (newMin <= dataMin && newMax >= dataMax) { zoom = null; return; }
    if (newMax - newMin < (dataMax - dataMin) * 0.02) return;   // floor at 2% of full range
    zoom = { xMin: newMin, xMax: newMax };
  }
  function onPointerDown(/** @type {PointerEvent} */ e) {
    if (!payoff.length || e.button !== 0) return;
    /** @type {any} */ const tgt = e.currentTarget;
    // Touch tap → toggle. If a tooltip is already pinned, the next
    // tap dismisses it (operator: "is there any way to hide it once
    // displayed"). Tap on a different spot pins to that spot. No
    // pan: native scroll on a chart-cell on mobile is rarely what
    // the operator wants when reading a value at a strike.
    if (e.pointerType === 'touch') {
      if (hover) { _dismissHover(); return; }
      _setHoverFromClientX(/** @type {SVGSVGElement} */ (tgt), e.clientX);
      return;
    }
    tgt.setPointerCapture?.(e.pointerId);
    pan = { startClientX: e.clientX, startMin: sMin, startMax: sMax };
  }
  function onPointerUp(/** @type {PointerEvent} */ e) {
    if (pan) {
      /** @type {any} */ const tgt = e.currentTarget;
      tgt.releasePointerCapture?.(e.pointerId);
    }
    pan = null;
  }
  function resetZoom() { zoom = null; pan = null; }

  // Y-axis ticks — 5 evenly spaced labels.
  const yTicks = $derived.by(() => {
    if (!payoff.length) return [];
    const { lo, hi } = yDomain;
    const n = 5;
    return Array.from({ length: n }, (_, i) => {
      const v = lo + ((hi - lo) * i) / (n - 1);
      return { v, y: yOf(v) };
    });
  });

  // X-axis ticks — sigma marks at every 0.5σ across ±spanSigmas
  // around the spot, when spanSigmas + spanPct are supplied (the API
  // returns both for auto-derived ranges). Each k-σ point sits at
  //   spot * (1 + k * spanPct / spanSigmas)
  // since the chart's spot range is ±spanPct and that range maps
  // 1-to-1 to ±spanSigmas. Falls back to evenly-spaced spot ticks
  // when spanSigmas isn't provided (operator-overridden span_pct).
  const xTicks = $derived.by(() => {
    if (!payoff.length) return [];
    if (spanSigmas > 0 && spanPct > 0 && spot > 0) {
      const ticks = [];
      // -spanSigmas → +spanSigmas in 0.5 steps. Round to single
      // decimal so floating math doesn't push 0 to 0.0000001.
      for (let k = -spanSigmas; k <= spanSigmas + 1e-9; k += 0.5) {
        const kRounded = Math.round(k * 2) / 2;
        const s = spot * (1 + (kRounded * spanPct) / spanSigmas);
        if (s < sMin - 1e-6 || s > sMax + 1e-6) continue;
        ticks.push({
          s,
          x: xOf(s),
          sigma: kRounded,
          label: kRounded === 0 ? '0' :
                 (kRounded > 0 ? '+' : '−') +
                 (Math.abs(kRounded) % 1 === 0
                   ? Math.abs(kRounded).toFixed(0)
                   : Math.abs(kRounded).toFixed(1)) + 'σ',
        });
      }
      return ticks;
    }
    // Fallback — evenly spaced spot prices when sigma metadata is
    // unavailable (custom span_pct).
    const n = 5;
    return Array.from({ length: n }, (_, i) => {
      const s = sMin + (sSpan * i) / (n - 1);
      return { s, x: xOf(s), sigma: null, label: s.toFixed(0) };
    });
  });
</script>

<div class="payoff-chart" style="--chart-h: {height}px">
  {#if !payoff.length}
    <div class="payoff-empty">
      No payoff data.
    </div>
  {:else}
    {#if isZoomed}
      <button type="button" class="payoff-reset"
              title="Reset zoom — return to the auto ±3σ range"
              onclick={resetZoom}>reset zoom</button>
    {/if}
    {#if onRefresh}
      <!-- Top-right refresh button. Absolutely positioned so the
           "Refresh" → "Refreshing…" text swap can never push the
           SVG / stat overlay / legend around. Width is locked to
           the wider "Refreshing…" string so the button itself also
           stays put across state changes. -->
      <button type="button"
              class="payoff-refresh"
              class:payoff-refresh-busy={loading}
              disabled={loading}
              title="Re-fetch spot, LTPs, Greeks, and the payoff curve now"
              aria-label="Refresh prices"
              onclick={() => onRefresh && onRefresh()}>
        <span class="payoff-refresh-label">
          {#if loading}Refreshing…{:else}↻ Refresh{/if}
        </span>
      </button>
    {/if}
    <!-- Top-left stat overlay — the chart's at-a-glance numerics so the
         operator doesn't have to glance at the Greeks / Risk cards just
         to read TDAY P&L or max profit. Pointer-events: none so the
         SVG hover / zoom / pan stay click-through. -->
    <!-- Stat overlay. aria-hidden was set so screen readers don't see
         the abbreviated keys; we override per-row with a `title=` so a
         hover/long-press surfaces the meaning of each label
         (operator: "what is meaning of σ"). -->
    <div class="payoff-stats">
      <div class="ps-row" title="Underlying spot price (current LTP)">
        <span class="ps-k">SPOT</span>
        <span class={'ps-v ps-spot ps-spot-' + spotDir}
              title={prevClose != null && prevClose > 0
                ? 'Prev close ₹' + prevClose.toLocaleString('en-IN', { maximumFractionDigits: 2 })
                  + ' · ' + (spot >= prevClose ? '+' : '−')
                  + Math.abs((spot / prevClose - 1) * 100).toFixed(2) + '%'
                : 'Spot — prev close unavailable'}>
          ₹{spot.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
        </span>
      </div>
      {#if curveAtSpot}
        <div class="ps-row"
             title={realizedPnl !== 0
               ? `Today's strategy P&L at the current spot — adjusted to match the dashboard's per-underlying ₹ exactly. ADJ row shows the offset folded in (closed-position realised + open-leg theoretical-vs-LTP gap).`
               : "Today's strategy P&L at the current spot — Black-Scholes value of all open legs minus entry cost"}>
          <span class="ps-k">TDAY</span>
          <span class={'ps-v ' + (curveAtSpot.today_value >= 0 ? 'ps-pos' : 'ps-neg')}>
            {fmtMoney(curveAtSpot.today_value)}
          </span>
        </div>
        {#if realizedPnl !== 0}
          <!-- ADJ = vertical offset folded into TDAY so the chart's
               value at the current spot equals the dashboard ₹ for
               this underlying. Two contributions:
                 - realised P&L from closed positions (qty=0)
                 - theoretical-vs-LTP gap on open legs (BS chart
                   pricing drifts from market LTP for illiquid
                   contracts) -->
          <div class="ps-row"
               title="Adjustment folded into TDAY so chart matches dashboard exactly. Includes realised P&L from today's closed positions + theoretical-vs-LTP gap on open legs.">
            <span class="ps-k">ADJ</span>
            <span class={'ps-v ' + (realizedPnl >= 0 ? 'ps-pos' : 'ps-neg')}>
              {fmtMoney(realizedPnl)}
            </span>
          </div>
        {/if}
        <div class="ps-row"
             title="Strategy P&L at expiry (intrinsic only) for the current spot — same vertical offset as TDAY.">
          <span class="ps-k">EXP</span>
          <span class={'ps-v ' + (curveAtSpot.expiry_value >= 0 ? 'ps-pos' : 'ps-neg')}>
            {fmtMoney(curveAtSpot.expiry_value)}
          </span>
        </div>
      {/if}
      {#if dte != null}
        <div class="ps-row" title="Days to expiry (calendar days remaining)">
          <span class="ps-k">DTE</span>
          <span class="ps-v">{Math.round(dte)}</span>
        </div>
      {/if}
      {#if ivProxy != null}
        <!-- σ = strategy's implied volatility (annualised, qty-weighted
             across the option legs). Drives Black-Scholes pricing for
             the today curve and the σ-tick spacing on the x-axis. -->
        <div class="ps-row"
             title="Implied volatility (annualised %) — qty-weighted IV across the option legs">
          <span class="ps-k">σ <span class="ps-k-hint">IV</span></span>
          <span class="ps-v">{(ivProxy * 100).toFixed(1)}%</span>
        </div>
      {/if}
      {#if legCount != null}
        <div class="ps-row" title="Number of legs in the strategy basket">
          <span class="ps-k">LEGS</span>
          <span class="ps-v">{legCount}</span>
        </div>
      {/if}
    </div>
    <svg viewBox="0 0 {W} {height}" preserveAspectRatio="none"
         class="payoff-svg" class:payoff-panning={pan !== null}
         role="img" aria-label="Option payoff diagram — wheel to zoom, drag to pan"
         onwheel={onWheel}
         onpointerdown={onPointerDown}
         onpointerup={onPointerUp}
         onpointermove={onPointerMove}
         onpointerleave={onPointerLeave}>
      <defs>
        <!-- Tooltip gradient — same #273552 → #1d2a44 vertical
             gradient as the .payoff-stats overlay (CSS) and the
             algo-status-card / OrderTicket surfaces. Lets the SVG
             hover-tooltip read as part of the same surface family
             without a separate flat-fill rect. -->
        <linearGradient id="payoff-tip-gradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stop-color="#273552" stop-opacity="0.95"/>
          <stop offset="100%" stop-color="#1d2a44" stop-opacity="0.95"/>
        </linearGradient>
      </defs>
      <!-- Profit / loss shading (under the curves so the lines pop) -->
      <path d={fillProfit} fill="rgba(74,222,128,0.10)" stroke="none"/>
      <path d={fillLoss}   fill="rgba(248,113,113,0.10)" stroke="none"/>

      <!-- Y-axis grid + labels. Halo (stroke + paint-order) lets the
           label punch cleanly through the horizontal grid line behind
           it without needing extra padding. Bumped from font-size 9
           muted-slate to font-size 11 light-blue + weight 600 — the
           earlier styling was too dim/small to read at a glance, and
           on mobile it disappeared into the chart background. -->
      {#each yTicks as t}
        <line x1={PAD_L} x2={W - PAD_R} y1={t.y} y2={t.y}
              stroke="rgba(200,216,240,0.10)" stroke-width="1"/>
        <text x={PAD_L - 6} y={t.y + 4} text-anchor="end"
              fill="#c8d8f0"
              stroke="#152033"
              stroke-width="3"
              paint-order="stroke fill"
              font-size="11" font-weight="600"
              font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">
          {fmtMoney(t.v)}
        </text>
      {/each}

      <!-- X-axis grid + labels — sigma marks at every 0.5σ across
           ±spanSigmas. Whole-sigma ticks (±1σ, ±2σ, …) get a
           stronger dashed line + brighter labels; half-sigma ticks
           subdued. Each tick gets BOTH the σ label (rotated -30°
           at the bottom axis) AND the actual spot price rendered
           vertically (rotated -90°) along the dashed line itself
           — so the operator can read either dimension at a glance
           without leaving the chart. -->
      {#each xTicks as xt}
        {@const wholeSigma = xt.sigma != null && xt.sigma % 1 === 0}
        {@const isCenter   = xt.sigma === 0}
        {#if !isCenter}
          <!-- σ-tick verticals: more subtle than before so the price
               labels rendered ON them stay the dominant visual.
               whole-σ: 0.30 → 0.18, half-σ: 0.14 → 0.07. -->
          <line x1={xt.x} x2={xt.x} y1={PAD_T} y2={height - PAD_B}
                stroke="rgba(200,216,240,{wholeSigma ? 0.18 : 0.07})"
                stroke-width="1"
                stroke-dasharray={wholeSigma ? '4 3' : '2 3'}/>
        {/if}
        {#if !isCenter}
          <!-- σ tick label at the bottom (rotated -30°). -->
          {@const ly = height - PAD_B + 10}
          <text x={xt.x} y={ly}
                text-anchor="end"
                transform="rotate(-30 {xt.x} {ly})"
                fill={wholeSigma ? '#fbbf24' : '#c8d8f0'}
                stroke="#152033"
                stroke-width="3"
                paint-order="stroke fill"
                font-size={wholeSigma ? 11 : 10}
                font-weight={wholeSigma ? 700 : 500}
                font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">
            {xt.label}
          </text>
          <!-- Vertical price label on the σ tick. Bumped from 11/10
               → 13/12 with a stronger halo (4 px stroke) and brighter
               whole-σ fill (#f9fafb white-ish, was #e2e8f0). The
               σ-line beneath was just made more subtle so these
               labels become the dominant read. -->
          {@const vx = xt.x + 5}
          {@const vy = height - PAD_B - 4}
          <text x={vx} y={vy}
                text-anchor="start"
                transform="rotate(-90 {vx} {vy})"
                fill={wholeSigma ? '#f9fafb' : '#e2e8f0'}
                stroke="#152033"
                stroke-width="4"
                paint-order="stroke fill"
                font-size={wholeSigma ? 13 : 12}
                font-weight={wholeSigma ? 700 : 600}
                font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">
            {xt.s.toFixed(0)}
          </text>
        {/if}
      {/each}

      <!-- Zero line — solid, slightly stronger than the grid -->
      <line x1={PAD_L} x2={W - PAD_R} y1={zeroY} y2={zeroY}
            stroke="rgba(255,255,255,0.25)" stroke-width="1"/>

      <!-- Spot vertical — DOTTED cyan (operator request). σ ticks are
           dashed at varying opacities, BE is dashed cream, today is
           solid amber, expiry is dashed sky-blue → spot stays
           visually distinct as the only DOTTED vertical. round line-
           caps render the dasharray "1 4" as a clean dot-grid. -->
      {#if spot > sMin && spot < sMax}
        <!-- Spot vertical kept dotted but very subtle (alpha 0.85
             → 0.30) so the dotted pattern is just-visible against
             the chart curves; the rotated SPOT-price label below
             carries the readable identity via its halo. -->
        <line x1={xOf(spot)} x2={xOf(spot)} y1={PAD_T} y2={height - PAD_B}
              stroke="rgba(125,211,252,0.30)" stroke-width="1.25"
              stroke-dasharray="1 4" stroke-linecap="round"/>
        <!-- Anchor 5 px to the right of the spot line — visible gap
             between the cyan vertical and the SPOT label glyphs. -->
        {@const sx = xOf(spot) + 5}
        {@const sy = height - PAD_B - 4}
        <text x={sx} y={sy}
              text-anchor="start"
              transform="rotate(-90 {sx} {sy})"
              fill="#7dd3fc"
              stroke="#152033"
              stroke-width="3"
              paint-order="stroke fill"
              font-size="11" font-weight="700"
              font-family="ui-monospace, SFMono-Regular, Menlo, monospace">
          SPOT {spot.toFixed(0)}
        </text>
      {/if}

      <!-- Breakeven markers — soft cream dashed verticals; multi-leg
           strategies (iron condor, butterfly) can produce two.
           BE PRICE rendered vertically on the line itself. -->
      {#each breakevenList as be}
        {#if be > sMin && be < sMax}
          <!-- Breakeven verticals use a soft cream / amber-200 palette
               (`#fde68a` ~ Tailwind amber-200). The pink/magenta tone
               we shipped first read as foreign next to the chart's
               amber + sky-blue + cyan family. Cream stays visually
               quiet but keeps the "important threshold" connotation;
               the heavier dash + bolder stroke compared to the σ
               grid still telegraphs this is the outcome-zero
               boundary, not a routine grid line. -->
          <!-- BE vertical: alpha 0.75 → 0.30, stroke-width 1.25 → 1
               so the line itself is very subtle; the rotated cream
               BE-price label still pops via its halo. -->
          <line x1={xOf(be)} x2={xOf(be)} y1={PAD_T} y2={height - PAD_B}
                stroke="rgba(253,230,138,0.30)" stroke-width="1"
                stroke-dasharray="5 3"/>
          <!-- BE label anchored near the BOTTOM of the chart, same
               convention as the σ-tick price labels: away from the
               top-left stat overlay + top-right Refresh button.
               5 px gap to the right of the dashed line so the BE
               glyphs aren't sitting on the cream stroke. -->
          {@const bx = xOf(be) + 5}
          {@const by = height - PAD_B - 4}
          <text x={bx} y={by}
                text-anchor="start"
                transform="rotate(-90 {bx} {by})"
                fill="#fde68a"
                stroke="#152033"
                stroke-width="3"
                paint-order="stroke fill"
                font-size="11" font-weight="700"
                font-family="ui-monospace, SFMono-Regular, Menlo, monospace">
            BE {be.toFixed(0)}
          </text>
        {/if}
      {/each}

      <!-- Spot vertical line removed by operator request (only σ
           ticks + breakevens remain as on-chart verticals). The
           current-P&L dot below still anchors at spot, and the
           SPOT readout in the top-left stat overlay carries the
           numeric value. -->

      <!-- Time-slice curves (between Today and Expiry) — drawn FIRST
           so the today + expiry anchor curves render on top. Dashed,
           thinner than the anchors, with HSL-interpolated colour
           from amber → sky so the operator reads them as a temporal
           gradient. -->
      {#each intermediatePaths as ip (ip.elapsed)}
        <path d={ip.d} fill="none" stroke={ip.color}
              stroke-width="1" stroke-dasharray="2 2"
              stroke-opacity="0.65"/>
      {/each}
      <!-- Expiry curve (dashed sky) -->
      <path d={pathExpiry} fill="none" stroke="#7dd3fc"
            stroke-width="1.25" stroke-dasharray="4 3" stroke-opacity="0.85"/>
      <!-- Today curve (solid amber, primary) -->
      <path d={pathToday}  fill="none" stroke="#fbbf24" stroke-width="1.75"/>

      <!-- Current P&L marker (dot at spot, today_value) -->
      {#if currentPnl != null && spot >= sMin && spot <= sMax}
        <circle cx={xOf(spot)} cy={yOf(currentPnl)} r="4"
                fill="#fbbf24" stroke="#0c1830" stroke-width="1.5"/>
      {/if}

      <!-- Hover crosshair + tooltip. Styling mirrors the top-left
           `.payoff-stats` overlay: same dark-navy/amber-22%-border
           shell, same SPOT/TDAY/EXP key/value layout, same muted-
           slate key colour + value tints. The two boxes read as a
           consistent family — the stat overlay shows numerics at
           the live spot, the tooltip shows them at hover-spot. -->
      {#if hover}
        <!-- Hover tooltip — operator: "leave extra space between
             label and number". Box widened 130 → 165 so labels
             (left, x=tx+10) and values (right-anchored x=tx+155)
             have ~30 px of breathing room even when the value is
             wide ("-₹1,500,000"). Fonts unchanged from previous
             slimming pass (11 / 14). -->
        {@const tx = Math.min(W - 165 - PAD_R, Math.max(PAD_L, hover.x + 10))}
        {@const ty = Math.max(PAD_T, hover.y - 58)}
        {@const tdCol = hover.today  >= 0 ? '#4ade80' : '#f87171'}
        {@const expCol = hover.expiry >= 0 ? '#4ade80' : '#f87171'}
        <line x1={hover.x} x2={hover.x} y1={PAD_T} y2={height - PAD_B}
              stroke="rgba(255,255,255,0.20)" stroke-width="1"/>
        <g>
          <!-- Click-anywhere-on-tooltip-to-close (operator: "instead
               of using X, pressing on the tooltip should close it").
               The visible × button is gone; the rect itself is the
               dismiss target. stopPropagation on pointerdown blocks
               the SVG-level pan setup so the click reliably reaches
               this rect's onclick handler. Cursor: pointer makes the
               affordance visible on hover.
               Row baselines back to 18 / 36 / 54 (no header strip
               needed without the ×); box height back to 60. -->
          <rect x={tx} y={ty} width="165" height="54" rx="6"
                fill="url(#payoff-tip-gradient)"
                stroke="rgba(251,191,36,0.30)" stroke-width="1"
                style="cursor: pointer;"
                pointer-events="all"
                onclick={(e) => { e.stopPropagation(); _dismissHover(); }}
                onpointerdown={(e) => { e.stopPropagation(); _dismissHover(); }}
                onkeydown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ' || e.key === 'Escape') {
                    e.preventDefault();
                    _dismissHover();
                  }
                }}
                role="button" tabindex="0"
                aria-label="Close tooltip — press anywhere"/>
          <!-- SPOT row — key in muted-slate, value in sky-cyan. -->
          <text x={tx + 10} y={ty + 16} fill="#fbbf24" fill-opacity="0.85"
                font-size="11" font-weight="700" font-family="monospace"
                letter-spacing="0.5"
                pointer-events="none">SPOT</text>
          <text x={tx + 155} y={ty + 16} fill="#7dd3fc"
                font-size="14" font-weight="700" text-anchor="end"
                font-family="monospace"
                pointer-events="none">{fmtSpot(hover.spot)}</text>
          <!-- TDAY / EXP rows — value coloured by sign (green/red). -->
          <text x={tx + 10} y={ty + 33} fill="#fbbf24" fill-opacity="0.85"
                font-size="11" font-weight="700" font-family="monospace"
                letter-spacing="0.5"
                pointer-events="none">TDAY</text>
          <text x={tx + 155} y={ty + 33} fill={tdCol}
                font-size="14" font-weight="700" text-anchor="end"
                font-family="monospace"
                pointer-events="none">{fmtMoney(hover.today)}</text>
          <text x={tx + 10} y={ty + 50} fill="#fbbf24" fill-opacity="0.85"
                font-size="11" font-weight="700" font-family="monospace"
                letter-spacing="0.5"
                pointer-events="none">EXP</text>
          <text x={tx + 155} y={ty + 50} fill={expCol}
                font-size="14" font-weight="700" text-anchor="end"
                font-family="monospace"
                pointer-events="none">{fmtMoney(hover.expiry)}</text>
        </g>
      {/if}
    </svg>
    <div class="payoff-legend">
      <span class="legend-item">
        <span class="legend-line legend-today"></span>
        Today (BS)
      </span>
      {#each intermediatePaths as ip (ip.elapsed)}
        <!-- Intermediate-DTE legend chips render in temporal order
             between Today and Expiry. Stroke colour matches the
             chart line so the operator can pair label → curve at
             a glance. -->
        <span class="legend-item">
          <span class="legend-line legend-mid"
                style="border-top-color: {ip.color}"></span>
          {ip.label}
        </span>
      {/each}
      <span class="legend-item">
        <span class="legend-line legend-expiry"></span>
        Expiry (intrinsic)
      </span>
      <span class="legend-item legend-be">
        <span class="legend-mark legend-be-mark"></span>
        Breakeven
      </span>
      <span class="legend-item">
        <span class="legend-mark legend-spot-mark"></span>
        Spot
      </span>
    </div>
  {/if}
</div>

<style>
  .payoff-chart {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-left: 3px solid #fbbf24;
    border-radius: 4px;
    padding: 6px 8px 8px;
    width: 100%;
    box-sizing: border-box;
    position: relative;
  }
  .payoff-svg {
    width: 100%;
    height: var(--chart-h, 280px);
    display: block;
    cursor: crosshair;
    touch-action: pan-y;
  }
  .payoff-svg.payoff-panning { cursor: grabbing; }
  .payoff-reset {
    position: absolute;
    top: 0.4rem;
    /* Sit immediately to the LEFT of the Refresh button (Refresh
       width 5.4rem + 0.6rem right offset + 0.3rem gap = 6.3rem) so
       the two top-right buttons never overlap. */
    right: 6.3rem;
    font-family: monospace;
    font-size: 0.5rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.10);
    color: #fbbf24;
    cursor: pointer;
    z-index: 1;
  }
  .payoff-reset:hover {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.65);
  }

  /* Refresh button — top-right corner of the chart. Width is locked
     so the "Refresh" / "Refreshing…" swap never reflows; the inner
     <span> centers either label inside the same fixed box. Position
     is absolute so the button + its state changes can never push the
     SVG / stat overlay / legend around. */
  .payoff-refresh {
    position: absolute;
    top: 0.4rem;
    right: 0.6rem;
    width: 5.4rem;
    height: 1.1rem;
    padding: 0;
    border-radius: 2px;
    border: 1px solid rgba(125,211,252,0.55);
    background: rgba(125,211,252,0.10);
    color: #7dd3fc;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    z-index: 2;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .payoff-refresh-label {
    /* Center the label inside the fixed-width button; tabular-nums
       keeps any digits steady if a future variant interleaves them. */
    display: inline-block;
    text-align: center;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .payoff-refresh:hover:not(:disabled) {
    background: rgba(125,211,252,0.22);
    border-color: rgba(125,211,252,0.85);
  }
  .payoff-refresh:disabled,
  .payoff-refresh-busy {
    cursor: progress;
    opacity: 0.7;
  }
  .payoff-empty {
    height: var(--chart-h, 280px);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #a3b9d0;
    font-size: 0.65rem;
    font-family: monospace;
  }
  .payoff-legend {
    display: flex;
    gap: 0.9rem;
    flex-wrap: wrap;
    padding-top: 0.4rem;
    font-size: 0.65rem;
    font-family: monospace;
    color: #c8d8f0;
    border-top: 1px solid rgba(255,255,255,0.05);
    margin-top: 0.3rem;
  }
  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .legend-line {
    width: 16px;
    height: 0;
  }
  .legend-today  { border-top: 2px solid #fbbf24; }
  .legend-expiry { border-top: 1.5px dashed #7dd3fc; }
  /* Intermediate-DTE swatch — the stroke colour comes inline via
     `style="border-top-color: …"` because each slice gets its own
     interpolated hsl(). Dashed, matching the chart curve. */
  .legend-mid    { border-top: 1.5px dashed #fbbf24; }
  .legend-mark {
    display: inline-block;
    width: 0;
    height: 12px;
  }
  .legend-spot-mark { border-left: 2px dotted #7dd3fc; }
  .legend-be-mark   { border-left: 2px dashed #fde68a; }
  /* Solid swatch for the spot legend — visually pairs with the
     SOLID cyan vertical drawn at the spot price (BE is dashed
     cream, σ ticks are dashed amber/light-blue). */

  /* On-chart stat overlay — reads the key numerics off the curve so
     the chart is self-contained. Sits top-left, semi-transparent,
     pointer-events disabled so it never blocks the SVG hover/zoom. */
  .payoff-stats {
    position: absolute;
    top: 0.5rem;
    left: 0.6rem;
    display: grid;
    grid-template-columns: max-content max-content;
    column-gap: 0.5rem;
    row-gap: 0.12rem;
    padding: 0.32rem 0.55rem;
    /* Match the algo theme's card palette — same gradient, amber
       border, soft shadow as `.algo-status-card` and the OrderTicket
       modal. Operator wanted the overlay and popup to read as part
       of the algo surface family, not a foreign element. */
    border-radius: 6px;
    background: linear-gradient(180deg, rgba(39,53,82,0.92) 0%, rgba(29,42,68,0.92) 100%);
    border: 1px solid rgba(251,191,36,0.30);
    box-shadow: 0 3px 10px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.08);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    /* Operator: "make the overlay look smaller, reduce the font size
       slightly". Bumped down 0.75rem → 0.65rem on values and 0.78 →
       0.7rem on keys; padding + gaps trimmed in proportion. Still
       large enough to read at a glance but takes ~25 % less vertical
       and horizontal space. */
    font-size: 0.65rem;
    line-height: 1.2;
    z-index: 1;
  }
  .ps-row {
    display: contents;
    cursor: help;
  }
  .ps-k {
    /* Amber label tier — same treatment as the OrderTicket modal's
       .ot-label so popups across the algo theme share a label
       colour scheme. Letter-spacing + opacity 0.85 mirror the
       OrderTicket variant. */
    color: #fbbf24;
    letter-spacing: 0.08em;
    font-size: 0.7rem;
    font-weight: 700;
    opacity: 0.85;
    align-self: center;
  }
  /* Inline hint after the σ glyph — small "IV" tag clarifies what
     the symbol means without giving up the canonical σ shorthand. */
  .ps-k-hint {
    margin-left: 0.25rem;
    font-size: 0.6rem;
    font-weight: 500;
    color: #fde68a;
    opacity: 0.75;
    letter-spacing: 0.06em;
  }
  .ps-v {
    text-align: right;
    font-weight: 700;
    color: #e2e8f0;
    font-variant-numeric: tabular-nums;
  }
  .ps-v.ps-spot { color: #7dd3fc; }
  /* Day-direction tint on the SPOT readout — green when above
     yesterday's close, red below. Falls through to the neutral
     cyan (`ps-spot-flat`) when prev_close is unavailable. */
  .ps-v.ps-spot-pos  { color: #4ade80; }
  .ps-v.ps-spot-neg  { color: #f87171; }
  .ps-v.ps-spot-flat { color: #7dd3fc; }
  .ps-v.ps-pos  { color: #4ade80; }
  .ps-v.ps-neg  { color: #f87171; }
</style>
