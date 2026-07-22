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
   *   expiryPnlOffset?: number,
   *   dayPnl?:      number|null,
   *   legsExpPnlAtSpot?: number|null,
   *   onRefresh?:   (() => void) | null,
   *   loading?:     boolean,
   *   prevClose?:   number|null,
   *   multiExpiry?: boolean,
   *   legSymbols?:  string[],
   *   spotAnchor?:  {contract:string, source:string, expiryISO?:string} | null,
   *   includeHoldings?: boolean | null,
   *   onToggleHoldings?: (() => void) | null,
   *   netCost?:     number|null,
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
    // Realised P&L offset applied only to the expiry curve — locked-in
    // gains from partially/fully closed F&O legs. Unlike `realizedPnl`
    // (which is the full BS-vs-broker MTM drift used for the today
    // curve), this carries only the closed-leg component so the expiry
    // curve shifts by exactly the realised gain without incorporating
    // any mark-to-market noise that has no meaning at settlement.
    expiryPnlOffset = /** @type {number} */ (0),
    // Sum of day_change_val (today's mark-to-market move) for the
    // enabled candidates. Rendered as the DAY row in the stat overlay
    // so the operator can compare it against the PositionStrip's P∆
    // chip. Subset relationship: when the basket covers EVERY open
    // position in the operator's book, DAY equals P∆ exactly. null /
    // 0 → DAY row hides.
    dayPnl = /** @type {number|null} */ (null),
    // Expiry P&L at the current spot computed from the legs grid's
    // canonical _legsExpPnlTotal helper — intrinsic-value formula
    // applied to the enabled, displayed candidate legs. When provided,
    // the stat overlay's EXP row shows this value instead of the
    // backend payoff curve's expiry_value, ensuring the chart overlay
    // matches the legs grid TOTAL and snapshot row exactly.
    // null / undefined → fall back to curveAtSpot.expiry_value (prior behaviour).
    legsExpPnlAtSpot = /** @type {number|null|undefined} */ (null),
    onRefresh  = /** @type {(() => void) | null} */ (null),
    loading    = false,
    prevClose  = /** @type {number|null|undefined} */ (null),
    multiExpiry = false,
    legSymbols = /** @type {string[]} */ ([]),
    // When the spot comes from a front-month MCX futures contract
    // rather than an index tick, this carries the contract symbol
    // and its expiry so the chip + roll warning can render.
    spotAnchor = /** @type {{contract:string, source:string, expiryISO?:string} | null} */ (null),
    // Optional Holdings toggle — renders a switch-style icon at the
    // right end of the chart legend when the caller wires both props.
    // Lets the operator flip whether equity-holding legs contribute
    // to the payoff curve + Greeks + Legs panel without leaving the
    // chart's eye-line. Omit both to hide the toggle entirely
    // (default — preserves the original legend layout for callers
    // that don't expose holdings).
    includeHoldings = /** @type {boolean | null} */ (null),
    onToggleHoldings = /** @type {(() => void) | null} */ (null),
    // Net strategy cost (total premium paid/received across all option legs).
    // Positive = net debit (operator paid premium); negative = net credit
    // (operator received premium). Rendered as a single horizontal dashed
    // amber annotation line at the cost level so the operator can read the
    // breakeven profile without looking at the Greeks overlay. null/0 = hide.
    netCost = /** @type {number|null} */ (null),
  } = $props();

  // Days until the anchor contract expires — used to decide whether
  // to flip the chip to amber "rolls in N days" mode. Computed from
  // expiryISO vs today's wall-clock date (UTC midnight comparison is
  // fine for a 3-day threshold; DST drift < 1 day won't matter here).
  const anchorDaysToExpiry = $derived.by(() => {
    if (!spotAnchor?.expiryISO) return null;
    try {
      const expMs  = new Date(spotAnchor.expiryISO + 'T00:00:00Z').getTime();
      const todayMs = new Date(new Date().toISOString().slice(0, 10) + 'T00:00:00Z').getTime();
      return Math.round((expMs - todayMs) / 86_400_000);
    } catch {
      return null;
    }
  });

  // True when the anchor is within 3 calendar days of expiry.
  const anchorRollingSoon = $derived(
    anchorDaysToExpiry !== null && anchorDaysToExpiry <= 3
  );

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
    const todayOff  = realizedPnl || 0;
    const expiryOff = expiryPnlOffset || 0;
    if (!todayOff && !expiryOff) return payoff;
    return payoff.map(p => ({
      spot:         p.spot,
      today_value:  p.today_value != null ? p.today_value + todayOff : null,
      expiry_value: p.expiry_value + expiryOff,
    }));
  });

  // Recompute breakevens from the SHIFTED expiry curve when there's
  // an offset — caller's `breakevens` prop came from the unshifted
  // backend curve, so its zero-crossings are off by `realizedPnl`
  // worth of vertical space when we plot the shifted curve.
  const adjustedBreakevens = $derived.by(() => {
    const todayOff  = realizedPnl || 0;
    const expiryOff = expiryPnlOffset || 0;
    if (!todayOff && !expiryOff) return null;
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

  // BE label pin positions — vertical text just above the chart baseline,
  // alternating sides per BE so adjacent labels never overlap each other
  // or the central spot marker. Inside the plot area, doesn't compete
  // with σ-tick price labels in the bottom padding band.
  /** @type {Array<{be:number,label:string,pinY:number,dx:number,anchor:string}>} */
  const bePins = $derived.by(() => {
    return breakevenList.map((be, i) => {
      const label = priceFmt(be);
      // All labels share the same Y baseline. Operator: "verticals
      // at the same position for breakeven lines the first label
      // before the line and the next label after the line".
      // Alternate side per BE — even-index labels sit BEFORE the
      // vertical line (left side), odd-index labels sit AFTER it
      // (right side). For the typical 2-BE iron condor / vertical
      // spread shape that's first BE on left, second BE on right
      // so the two labels never overlap each other or the chart's
      // central spot marker.
      const side = i % 2 === 0 ? 'left' : 'right';
      // Right-side labels nudged further out (+10 vs -4 on the
      // left). Operator: "the right side label move towards right
      // a little bit". The asymmetry is intentional — left labels
      // sit close to their line because the chart's left padding
      // gives them room; right labels need extra breathing room
      // so they don't crowd into the chart's right edge / spot
      // marker area.
      const dx = side === 'left' ? -4 : 10;
      const pinY = (height - PAD_B) - 6;
      const anchor = 'start';
      return { be, label, pinY, dx, anchor };
    });
  });

  /** @type {{x:number,y:number,spot:number,today:number,expiry:number}|null} */
  let hover = $state(null);

  // ── Geometry ──────────────────────────────────────────────────────
  const W = 720;
  // PAD_L widened to 36 to accommodate horizontal left-edge Y-axis labels.
  // PAD_B widened to 36 — milestone σ ticks (±1σ / ±2σ / ±2.5σ) draw a
  // stacked two-line label (colored σ-tag above the price) so the chart
  // bottom needs ~26 px clearance below the plot baseline. Non-milestone
  // whole-σ ticks still use a single-line label that fits in the same
  // budget.
  // Top padding tight since BE labels no longer overlap the top edge —
  // they sit BELOW the x-axis baseline now (see bePins above).
  // PAD_B bumped 36 → 50 to make room for BE labels under the σ-tick
  // price labels (σ row at +12/+25, BE row at +28 from baseline).
  const PAD_L = 36, PAD_R = 12, PAD_T = 14, PAD_B = 50;
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

  // Net strategy cost annotation — Y position of the cost/credit level.
  // For a net debit (paid premium), the break-even-at-expiry line sits at
  //   y = −netCost (the P&L when intrinsic = cost, i.e. exactly breaks even).
  // For a net credit (received premium), it sits at y = +|netCost|.
  // Only render when netCost is non-zero and the Y level is within the chart.
  const _netCostY = $derived.by(() => {
    if (!netCost || netCost === 0 || !payoff.length) return null;
    const level = -netCost;  // on the payoff P&L axis: debit → negative level, credit → positive
    const y = yOf(level);
    // Suppress if outside the visible plot area (e.g. very deep debit strategy
    // where the cost line is off the bottom of the chart).
    if (y < PAD_T || y > height - PAD_B) return null;
    return { y, level, label: netCost > 0 ? `Cost ₹${aggFmt(netCost)}` : `Credit ₹${aggFmt(-netCost)}` };
  });

  // True when every payoff point carries a real today_value (not null/undefined).
  // The client-side intrinsic stub sets today_value: null to signal that BS
  // pricing hasn't arrived yet from the backend. Suppressing the path here
  // prevents a false flat-line at zero from rendering before real values land.
  const hasTodayValues = $derived(
    adjustedPayoff.length > 0 && adjustedPayoff.every(p => p.today_value != null)
  );

  // SVG paths — read from adjustedPayoff so the curve renders WITH
  // the realised-P&L offset applied. At realizedPnl=0 this equals
  // payoff (no-op pass-through).
  const pathToday = $derived.by(() => {
    if (!adjustedPayoff.length || !hasTodayValues) return '';
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

  import { untrack } from 'svelte';
  import { priceFmt, aggFmt, aggCompact } from '$lib/format';
  import LegLabel from '$lib/LegLabel.svelte';
  import { createChartRefreshPulse } from '$lib/data/chartRefreshPulse.svelte.js';

  const _pulse = createChartRefreshPulse();
  // Fire when payoff data changes — but NOT on hover / zoom / pan (those
  // don't change the payoff prop identity). `payoff` is the canonical
  // data prop; spot changes are derived display, not new data.
  $effect(() => {
    if (payoff.length) untrack(() => _pulse.notify('payoff'));
  });

  // Compact axis label for the Y axis — e.g. "+50K", "0", "-10K".
  // Keeps left-edge labels short enough to fit in PAD_L budget.
  function _axisFmt(/** @type {number} */ v) {
    if (v === 0) return '0';
    const sign = v > 0 ? '+' : '';
    return sign + aggCompact(v);
  }

  // Profit + loss zones — shade above and below zero on the today curve
  // up to the chart bounds. Two filled paths whose top/bottom rides the
  // today curve and whose other edge is the chart's boundary.
  const fillProfit = $derived.by(() => {
    if (!adjustedPayoff.length || !hasTodayValues) return '';
    const top = adjustedPayoff.map(p => `${xOf(p.spot).toFixed(1)},${yOf(Math.max(0, p.today_value)).toFixed(1)}`);
    const lastX  = xOf(adjustedPayoff[adjustedPayoff.length - 1].spot).toFixed(1);
    const firstX = xOf(adjustedPayoff[0].spot).toFixed(1);
    return `M${firstX},${zeroY.toFixed(1)} L${top.join(' L')} L${lastX},${zeroY.toFixed(1)} Z`;
  });
  const fillLoss = $derived.by(() => {
    if (!adjustedPayoff.length || !hasTodayValues) return '';
    const bot = adjustedPayoff.map(p => `${xOf(p.spot).toFixed(1)},${yOf(Math.min(0, p.today_value)).toFixed(1)}`);
    const lastX  = xOf(adjustedPayoff[adjustedPayoff.length - 1].spot).toFixed(1);
    const firstX = xOf(adjustedPayoff[0].spot).toFixed(1);
    return `M${firstX},${zeroY.toFixed(1)} L${bot.join(' L')} L${lastX},${zeroY.toFixed(1)} Z`;
  });

  function fmtMoney(/** @type {number} */ v) {
    return `₹${aggFmt(v)}`;
  }
  function fmtSpot(/** @type {number} */ v) {
    return `₹${priceFmt(v)}`;
  }

  // After dismiss, suppress hover for a short window so the cursor's
  // pointermove (still on the chart since the operator just clicked
  // there) doesn't immediately re-create the tooltip — the visible
  // glitch was "click sometimes works, sometimes not", actually a
  // dismiss-then-instant-rehover. 350 ms gives the operator time to
  // move the cursor away if they don't want the tooltip back; if
  // they DO want it back, any movement after the window re-arms.
  let _hoverSuppressUntil = 0;
  // Click-to-pin — operator: "make this default action for all the
  // charts". When pinned, pointermove + pointerleave don't change the
  // popup until the operator clicks again (or hits Esc / the × close
  // button). Touch tap toggles via onPointerDown; desktop click goes
  // through the click event so the existing pan/drag gestures aren't
  // accidentally treated as taps.
  let pinned = $state(false);
  function _dismissHover() {
    hover = null;
    pinned = false;
    _hoverSuppressUntil = Date.now() + 350;
  }

  // Re-snap a pinned hover tooltip whenever the underlying curve
  // changes (e.g. the operator toggles a leg in the Candidates
  // panel and strategy reloads). Without this, the popup keeps
  // displaying the today/expiry values captured at click time,
  // so a stale tooltip lingers showing the OLD curve's P&L at
  // the hovered spot.
  //
  // untrack() on the hover read so this $effect re-runs ONLY
  // when adjustedPayoff changes — not on every hover update.
  // Without untrack the write below (`hover = {...}`) would
  // re-trigger the effect infinitely.
  $effect(() => {
    const src = adjustedPayoff;
    untrack(() => {
      if (!hover || !src.length) return;
      const hoverSpot = hover.spot;
      let best = src[0];
      let bestDiff = Math.abs(best.spot - hoverSpot);
      for (const p of src) {
        const d = Math.abs(p.spot - hoverSpot);
        if (d < bestDiff) { best = p; bestDiff = d; }
      }
      hover = {
        x:      hover.x,
        y:      yOf(best.today_value != null ? best.today_value : best.expiry_value),
        spot:   best.spot,
        today:  best.today_value,
        expiry: best.expiry_value,
      };
    });
  });

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
      x: xOf(best.spot), y: yOf(best.today_value != null ? best.today_value : best.expiry_value),
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
      pinned = false;
      return;
    }
    if (pinned) return;
    _setHoverFromClientX(svg, e.clientX);
  }
  // Mouse leave clears hover unless pinned. Touch keeps it pinned
  // until the operator taps again (toggle behaviour, see onPointerDown).
  function onPointerLeave(/** @type {PointerEvent} */ e) {
    if (pinned) return;
    if (e.pointerType !== 'touch') hover = null;
  }
  // Desktop click → toggle pin. Browsers only fire `click` when there's
  // no significant drag between pointerdown and pointerup, so the
  // existing pan/zoom flow doesn't accidentally pin on every release.
  function onClick(/** @type {MouseEvent} */ e) {
    if (!payoff.length || pan) return;
    if (pinned) { _dismissHover(); return; }
    _setHoverFromClientX(/** @type {SVGSVGElement} */ (e.currentTarget), e.clientX);
    if (hover) pinned = true;
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
    // Clear any residual hover tooltip on click start so the click
    // never has a popup flickering (operator: "popup appears and
    // disappears when clicking under refresh").
    hover = null;
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

  // Spot x-coordinate — used for the Y-label chips column and the
  // spot price label so they share a single vertical axis.
  const spotX = $derived(xOf(spot));

  // X-axis ticks — sigma marks at every 0.5σ across ±spanSigmas
  // around the spot, when spanSigmas + spanPct are supplied (the API
  // returns both for auto-derived ranges). Each k-σ point sits at
  //   spot * (1 + k * spanPct / spanSigmas)
  // since the chart's spot range is ±spanPct and that range maps
  // 1-to-1 to ±spanSigmas. Falls back to evenly-spaced spot ticks
  // when spanSigmas isn't provided (operator-overridden span_pct).
  const _xTicksRaw = $derived.by(() => {
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

  // Cache the last non-empty xTicks so the σ axis doesn't flicker on
  // refresh when `payoff` briefly transitions through empty (parent
  // strategy state cycles when fetchStrategyAnalytics is in flight or
  // legs reactivity briefly empties cleanLegs). Without this the σ
  // verticals + their rotated price labels blink off then back on.
  let _stickyXTicks = $state(/** @type {any[]} */ ([]));
  $effect(() => {
    if (_xTicksRaw && _xTicksRaw.length > 0) {
      _stickyXTicks = _xTicksRaw;
    }
  });
  const xTicks = $derived(
    _xTicksRaw.length > 0 ? _xTicksRaw : _stickyXTicks
  );
</script>

<div class="payoff-chart" style="--chart-h: {height}px">
  {#if (legCount ?? 0) === 0 && !payoff.length}
    <!-- No legs selected — chart has nothing to plot. Tell the
         operator instead of leaving a stale "Resolving spot…" message
         that implied an in-flight fetch when there's actually nothing
         to fetch. -->
    <div class="payoff-empty">
      Pick legs to see payoff.
    </div>
  {:else if loading && (!payoff.length || spot == null)}
    <!-- Loading state — shown while the backend is actively resolving
         a real spot price with at least one leg in flight. Suppresses
         the brief "fallback strike" flash for MCX commodities when
         the instruments cache is cold and the spot resolver falls
         back to the strike. -->
    <div class="payoff-empty">
      Resolving spot…
    </div>
  {:else if !payoff.length}
    <div class="payoff-empty">
      No payoff data.
    </div>
  {:else}
    {#if isZoomed}
      <button type="button" class="payoff-reset"
              aria-label="Reset zoom"
              onclick={resetZoom}>reset zoom</button>
    {/if}
    <!-- RefreshButton is now rendered by the parent page in the card
         header (canonical icon placement) — never on the chart canvas.
         The legacy `onRefresh` prop remains so existing callsites stay
         compatible without re-wiring; the prop just isn't surfaced as
         a chart-overlay control any more. -->

    <!-- Top-left stat overlay — the chart's at-a-glance numerics so the
         operator doesn't have to glance at the Greeks / Risk cards just
         to read TDAY P&L or max profit. Pointer-events: none so the
         SVG hover / zoom / pan stay click-through. -->
    <!-- Stat overlay. aria-hidden was set so screen readers don't see
         the abbreviated keys; we override per-row with a `title=` so a
         hover/long-press surfaces the meaning of each label
         (operator: "what is meaning of σ"). -->
    <!-- SPOT is the first row of the overlay so every other value
         (TDAY / EXP / DTE / σ) is anchored to the price they're
         derived from. Sign-tinted by day direction (green above
         yesterday's close, red below, cyan when prev_close is
         unknown). Mirrors the top-of-chart SVG SPOT label and the
         dashboard's directional palette. -->
    <div class="payoff-stats">
      {#if spot != null}
        <div class="ps-row"
             title={spotAnchor?.source === 'futures'
               ? `Spot anchor: ${spotAnchor.contract} (front-month MCX future). True MCX spot isn't published. Cost-of-carry may differ from spot by ₹50-200.`
               : "Current spot price for the underlying — anchor for every other stat in this overlay"}>
          <span class="ps-k">SPOT</span>
          <span class={'ps-v ps-spot-' + spotDir}>{fmtSpot(spot)}</span>
        </div>
      {/if}
      {#if prevClose != null && Number.isFinite(prevClose) && prevClose > 0}
        <!-- Yesterday's close — anchors the SPOT colour direction and
             gives the operator a quick read on the day's spot drift
             without comparing the SPOT row to a number stored in their
             head. Bloomberg / TWS / Kite all show this. -->
        <div class="ps-row" title="Previous-session close for the underlying — anchor for today's spot drift">
          <span class="ps-k">CLOSE</span>
          <span class="ps-v ps-flat">{fmtSpot(prevClose)}</span>
        </div>
      {/if}
      {#if dayPnl != null && dayPnl !== 0}
        <!-- DAY P&L row — sum of today's mark-to-market change across
             enabled candidates. Reconciles with the PositionStrip's
             P∆ chip when the basket covers every position in the
             book. Operator can scan TODAY (lifetime P&L at spot) vs DAY P&L (today's
             intraday move) at a glance. -->
        <div class="ps-row"
             title="Today's mark-to-market change on enabled basket positions (sum of day_change_val). Compare to the PositionStrip's P∆ chip — they match exactly when the basket covers every open position.">
          <span class="ps-k">DAY P&amp;L</span>
          <span class={'ps-v ' + (dayPnl >= 0 ? 'ps-pos' : 'ps-neg')}>
            {fmtMoney(dayPnl)}
          </span>
        </div>
      {/if}
      {#if curveAtSpot}
        {#if curveAtSpot.today_value != null}
        <div class="ps-row"
             title={realizedPnl !== 0
               ? `Position lifetime P&L at the current spot (open + closed legs combined). Adjusted to match the dashboard's per-underlying ₹ exactly. ADJ row shows the offset folded in.`
               : "Position lifetime P&L at the current spot — Black-Scholes value of all open legs minus entry cost. NOT today's intraday move — use the DAY P&L row above for that."}>
          <span class="ps-k">TODAY</span>
          <span class={'ps-v ' + ((curveAtSpot?.today_value ?? 0) >= 0 ? 'ps-pos' : 'ps-neg')}>
            {fmtMoney(curveAtSpot?.today_value)}
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
               title="Adjustment folded into TODAY so chart matches dashboard exactly. Includes realised P&L from today's closed positions + theoretical-vs-LTP gap on open legs.">
            <span class="ps-k">ADJ</span>
            <span class={'ps-v ' + (realizedPnl >= 0 ? 'ps-pos' : 'ps-neg')}>
              {fmtMoney(realizedPnl)}
            </span>
          </div>
        {/if}
        {/if}
        {@const _expDisplayVal = (legsExpPnlAtSpot != null && Number.isFinite(legsExpPnlAtSpot))
          ? legsExpPnlAtSpot
          : curveAtSpot.expiry_value}
        <div class="ps-row"
             title={legsExpPnlAtSpot != null
               ? 'Strategy P&L if every open leg expired RIGHT NOW at the current spot — intrinsic value minus cost basis, summed across the enabled legs. SSOT shared with the legs grid TOTAL and snapshot Exp P&L column.'
               : 'Strategy P&L at expiry (intrinsic only) for the current spot — same vertical offset as TODAY.'}>
          <span class="ps-k">EXP</span>
          <span class={'ps-v ' + (_expDisplayVal >= 0 ? 'ps-pos' : 'ps-neg')}>
            {fmtMoney(_expDisplayVal)}
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

    <!--
      SVG stack wrapper — both SVGs sit absolute inside a
      relatively-positioned box whose dimensions are pinned to
      --chart-h. On viewport rotation (portrait ↔ landscape) the
      wrapper resizes and BOTH SVGs reflow simultaneously, so the
      bg / fg layers stay pixel-aligned. Without the wrapper, the
      fg SVG (absolute top/left/right) could race with the bg SVG
      (relative width:100%) on iOS Safari rotation and end up with
      different effective widths — the garble the operator was
      seeing was the fg curve drifting off the bg coordinate grid.
    -->
    <div class="payoff-svg-stack {_pulse.classOf('payoff')}">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <!-- Payoff SVG: wheel-zoom + drag-pan + click-pin are pointer-native.
         role="application" communicates this to AT; keyboard zoom/pan is
         not practical for this chart type. -->
    <svg viewBox="0 0 {W} {height}" preserveAspectRatio="none"
         class="payoff-svg" class:payoff-panning={pan !== null}
         role="application" aria-label="Option payoff diagram — wheel to zoom, drag to pan, click to pin"
         onwheel={onWheel}
         onpointerdown={onPointerDown}
         onpointerup={onPointerUp}
         onpointermove={onPointerMove}
         onpointerleave={onPointerLeave}
         onclick={onClick}>
      <!-- Plot-area background tint — first child so it sits behind
           profit/loss shading, grid lines, and payoff curves.
           --chart-bg-tint in app.css. -->
      <rect class="chart-bg" x={PAD_L} y={PAD_T} width={innerW} height={innerH}
            fill="var(--chart-bg-tint)" rx="0"/>
      <!-- Profit / loss shading (under the curves so the lines pop) -->
      <path d={fillProfit} fill="rgba(74,222,128,0.10)" stroke="none" class="data-path"/>
      <path d={fillLoss}   fill="rgba(248,113,113,0.10)" stroke="none" class="data-path"/>

      <!-- Y-axis grid lines only — left-edge tick marks and faint
           numeric labels are gone; the spot-vertical chip column is
           the sole source of P&L axis values. PAD_L tightened so
           the chart uses the recovered horizontal space. -->
      {#each yTicks as t}
        <line x1={PAD_L} x2={W - PAD_R} y1={t.y} y2={t.y}
              class={Math.abs(t.v) < 0.5 ? 'chart-grid-zero' : 'chart-grid-line'}/>
      {/each}

      <!-- X-axis grid — sigma tick lines at every 0.5σ across ±spanSigmas.
           Milestone σ levels (±1σ / ±2σ / ±2.5σ) get color-coded dotted
           verticals + a stacked two-line label (σ-tag above price). The
           color scheme is the classic risk-band gradient: ±1σ emerald
           (~68% containment, "typical move"), ±2σ amber (~95%, "wider
           than typical"), ±2.5σ rose (~98.8%, "tail event"). Non-milestone
           whole-σ ticks keep the subtle steel grid + single-line price.
           Half-σ ticks (0.5, 1.5) are reference grid only.
           The center tick (0σ = spot) draws via the spot-line block below;
           skip it here to avoid overdrawing. -->
      {#each xTicks as xt}
        {@const wholeSigma  = xt.sigma != null && xt.sigma % 1 === 0}
        {@const isCenter    = xt.sigma === 0}
        {@const absSigma    = xt.sigma != null ? Math.abs(xt.sigma) : null}
        {@const isMilestone = absSigma === 1 || absSigma === 2 || absSigma === 2.5}
        {@const mColor      = absSigma === 1   ? 'var(--c-long)'
                            : absSigma === 2   ? 'var(--c-action)'
                            : absSigma === 2.5 ? 'var(--c-short)'
                            : null}
        {#if !isCenter}
          {#if isMilestone}
            <!-- Milestone σ vertical — color-coded dotted line, kept at
                 the LEAST prominent tier per operator request ("make
                 spot most prominent, breakeven prominent, sigma least
                 prominent"). Hairline 0.75px stroke + 0.28 alpha so the
                 σ bands read as a quiet risk-zone reference rather than
                 a structural axis competing with the curves / spot /
                 breakeven lines on top. -->
            <line x1={xt.x} x2={xt.x} y1={PAD_T} y2={height - PAD_B}
                  stroke={mColor} stroke-width="0.75"
                  stroke-dasharray="2 4" stroke-opacity="0.28"/>
          {:else}
            <!-- Neutral grid: even quieter than milestone sigmas — kept
                 deliberately faint so the σ family stays the
                 least-prominent tier. -->
            <line x1={xt.x} x2={xt.x} y1={PAD_T} y2={height - PAD_B}
                  stroke={wholeSigma ? 'rgba(200,216,240,0.10)' : 'rgba(200,216,240,0.06)'}
                  stroke-width="0.75"
                  stroke-dasharray={wholeSigma ? '2 4' : '1 5'}/>
          {/if}
        {/if}
        {#if isMilestone}
          <!-- Stacked label: colored σ-tag (top) + price (bottom). The
               σ-tag inherits the milestone color so each band reads as a
               single visual unit. -->
          <text x={xt.x} y={height - PAD_B + 12}
                text-anchor="middle"
                fill={mColor}
                font-size="10" font-weight="700"
                style="font-family: var(--font-numeric)">
            {xt.label}
          </text>
          <text x={xt.x} y={height - PAD_B + 25}
                text-anchor="middle"
                fill={mColor}
                font-size="10" font-weight="600"
                style="font-family: var(--font-numeric); font-variant-numeric: tabular-nums">
            {Math.round(xt.s).toLocaleString('en-IN')}
          </text>
        {:else if wholeSigma && !isCenter}
          <!-- Non-milestone whole-σ price label (only renders when
               spanSigmas > 2 so we'd see ±3σ etc; typical default
               spanSigmas=2.5 means every whole-σ is a milestone). -->
          <text x={xt.x} y={height - PAD_B + 14}
                text-anchor="middle"
                fill="#c8d8f0"
                font-size="11" font-weight="600"
                style="font-family: var(--font-numeric); font-variant-numeric: tabular-nums">
            {Math.round(xt.s).toLocaleString('en-IN')}
          </text>
        {/if}
      {/each}

      <!-- Zero line — solid, slightly stronger than the grid -->
      <line x1={PAD_L} x2={W - PAD_R} y1={zeroY} y2={zeroY}
            stroke="rgba(255,255,255,0.25)" stroke-width="1"/>

      <!-- Net strategy cost annotation — thin dashed amber horizontal at the
           cost/credit level. Debit strategies: line sits below zero (loss zone)
           at −netCost; the breakeven at expiry is exactly where the expiry
           curve crosses this line. Credit strategies: line sits above zero
           (profit zone) at +|netCost| — the max profit if all legs expire OTM.
           Omitted when netCost is zero or out of the chart Y range. -->
      {#if _netCostY != null}
        <line x1={PAD_L} x2={W - PAD_R} y1={_netCostY.y} y2={_netCostY.y}
              stroke="#fbbf24" stroke-width="0.75"
              stroke-dasharray="4 3" stroke-opacity="0.55"
              pointer-events="none"/>
        <text x={W - PAD_R - 2} y={_netCostY.y - 3}
              text-anchor="end"
              fill="#fbbf24" fill-opacity="0.70"
              font-size="9" font-weight="600"
              style="font-family: var(--font-numeric); font-variant-numeric: tabular-nums"
              pointer-events="none">
          {_netCostY.label}
        </text>
      {/if}

      <!-- Breakeven markers — full-height amber vertical line + a
           horizontal pill pinned ABOVE the chart top edge so the
           label reads left-to-right without any head-tilt.
           A thin connector line drops from the pill bottom to PAD_T
           to visually link pill → vertical. Two BEs within 60 px
           stack vertically (level-0 at PAD_T-14, level-1 at PAD_T-36)
           so their pills never collide. -->
      {#each bePins as pin}
        {#if pin.be > sMin && pin.be < sMax}
          {@const bx = xOf(pin.be)}
          <!-- Full-height amber breakeven vertical — middle prominence
               tier. 1.5px stroke at 0.65 alpha sits above the σ band
               grid but quieter than the bright cyan spot line so the
               eye reads spot → breakevens → σ-band in descending
               order of visual weight. -->
          <line x1={bx} x2={bx} y1={PAD_T} y2={height - PAD_B}
                stroke="#fbbf24" stroke-width="1.5"
                stroke-opacity="0.65"
                stroke-dasharray="6 3"/>
          <!-- Breakeven × curve intersection — Kite-style dart at
               (bx, zeroY). Tiny inner pin + a snug halo so the
               marker reads as a quiet secondary anchor under the
               brighter spot dart. -->
          <circle cx={bx} cy={zeroY} r="4"
                  fill="rgba(251, 191, 36, 0.12)"
                  pointer-events="none"/>
          <circle cx={bx} cy={zeroY} r="3.5"
                  fill="none" stroke="#fbbf24" stroke-width="1"
                  stroke-opacity="0.55"
                  pointer-events="none"/>
          <circle cx={bx} cy={zeroY} r="2.25"
                  fill="#fbbf24" stroke="#0c1830" stroke-width="0.75"
                  fill-opacity="0.85"
                  pointer-events="none"/>
          <!-- BE label — vertical (reads bottom-to-top), pivot 4px
               left/right of the BE line so adjacent BEs alternate
               sides and never collide with each other or with the
               spot marker between them. text-anchor="start" + a
               -90° rotation around the pivot makes the text grow
               UPWARD from pinY into the plot area. -->
          <text x={bx + pin.dx} y={pin.pinY}
                text-anchor={pin.anchor}
                transform="rotate(-90 {bx + pin.dx} {pin.pinY})"
                fill="#fbbf24"
                stroke="#0c1830"
                stroke-width="3"
                paint-order="stroke fill"
                font-size="10" font-weight="700"
                style="font-family: var(--font-numeric)">
            {pin.label}
          </text>
        {/if}
      {/each}

      <!-- Time-slice curves (between Today and Expiry) — drawn FIRST
           so the today + expiry anchor curves render on top. Dashed,
           thinner than the anchors, with HSL-interpolated colour
           from amber → sky so the operator reads them as a temporal
           gradient. -->
      {#each intermediatePaths as ip (ip.elapsed)}
        <path d={ip.d} fill="none" stroke={ip.color}
              stroke-width="1" stroke-dasharray="2 2"
              stroke-opacity="0.65" class="data-path"/>
      {/each}
      <!-- Expiry curve (dashed sky) -->
      <path d={pathExpiry} fill="none" stroke="#7dd3fc"
            stroke-width="1.25" stroke-dasharray="4 3" stroke-opacity="0.85"
            class="data-path"/>
      <!-- Today curve (solid amber, primary) -->
      <path d={pathToday}  fill="none" stroke="#fbbf24" stroke-width="1.75"
            class="data-path"/>

      <!-- Spot × today-curve dart — operator: "The one intersection
           point between the spot price line and today's payoff line
           should have the filled circle with outer circle." Two
           concentric circles in the today curve's amber hue so the
           marker ties to its curve. Filled inner + outer ring; halo
           dropped per "outer circle close to inner". -->
      {#if currentPnl != null && spot >= sMin && spot <= sMax}
        <circle cx={spotX} cy={yOf(currentPnl)} r="6"
                fill="none" stroke="#fbbf24" stroke-width="1.25"
                stroke-opacity="0.55"
                pointer-events="none"/>
        <circle cx={spotX} cy={yOf(currentPnl)} r="3"
                fill="#fbbf24" fill-opacity="0.85"
                stroke="#0c1830" stroke-width="1"
                pointer-events="none"/>
      {/if}

      <!-- Spot × expiry-curve dart — same two-circle pattern in the
           expiry curve's sky-blue hue so the operator can tell at a
           glance which curve they're reading the value from
           (operator: "Similarly the spot price line and expiry curve
           intersection point"). -->
      {#if curveAtSpot != null && spot >= sMin && spot <= sMax}
        <circle cx={spotX} cy={yOf(curveAtSpot.expiry_value)} r="6"
                fill="none" stroke="#7dd3fc" stroke-width="1.25"
                stroke-opacity="0.55"
                pointer-events="none"/>
        <circle cx={spotX} cy={yOf(curveAtSpot.expiry_value)} r="3"
                fill="#7dd3fc" fill-opacity="0.85"
                stroke="#0c1830" stroke-width="1"
                pointer-events="none"/>
      {/if}

      <!-- Spot vertical line — operator: "remove decoration for spot
           price line. but still make more prominent". No dart sits
           on the line itself; instead the line gets a halo (wider
           translucent cyan underneath) + a sharp solid stroke on top
           so it reads as the most prominent structural marker on the
           chart without any pin clutter. Halo glow widens the
           apparent weight without making the inner stroke fat. -->
      {#if spot > sMin && spot < sMax}
        <!-- Halo glow — trimmed to 3 px α 0.10 per operator: "make
             the spot price line and darts less prominent". -->
        <line x1={spotX} x2={spotX} y1={PAD_T} y2={height - PAD_B}
              stroke="#22d3ee" stroke-width="3"
              stroke-opacity="0.10"
              pointer-events="none"/>
        <!-- Inner solid stroke — thinner + lower alpha so the
             spot line reads as a quieter reference. -->
        <line x1={spotX} x2={spotX} y1={PAD_T} y2={height - PAD_B}
              stroke="#22d3ee" stroke-width="1.25" stroke-opacity="0.65"/>
      {/if}

      <!-- Y-axis labels — left-edge, horizontal. One label per yTicks
           entry (including the zero row). No navy-rect chip or border;
           labels sit just outside the plot area on a clean background.
           text-anchor="end" anchors the right edge of each label to
           PAD_L-6 so labels hug the plot frame without overlapping it.
           Convention: TradingView / IBKR / Sensibull all use horizontal
           left-edge P&L axis labels. -->
      {#each yTicks as t}
        {#if t.y > PAD_T + 8 && t.y < height - PAD_B - 8}
          <text x={PAD_L - 6} y={t.y + 4}
                text-anchor="end"
                fill="#c8d8f0"
                font-size="11" font-weight="600"
                style="font-family: var(--font-numeric); font-variant-numeric: tabular-nums">
            {_axisFmt(t.v)}
          </text>
        {/if}
      {/each}

      <!-- Z-layer 11: Spot price — operator: "remove chip for break
           even text decoration. add it to spot price." Cyan pill
           centered on the spot vertical, identical shape to the
           breakeven pill the chart used to wear. Navy backing
           rect masks any σ tick label behind it; cyan fill +
           dark border with bold dark numerals so it reads as
           THE prominent reference marker on the chart. -->
      {#if spot > 0 && spot > sMin && spot < sMax}
        {@const spotLabel = spot.toFixed(0)}
        {@const spotChipW = spotLabel.length * 8 + 14}
        {@const spotChipH = 18}
        {@const spotChipY = PAD_T + 3}
        <!-- Navy mask underneath in case the pill overlaps a σ label. -->
        <rect x={spotX - spotChipW / 2 - 1} y={spotChipY - 1}
              width={spotChipW + 2} height={spotChipH + 2} rx="5"
              fill="#0d1829"/>
        <!-- Cyan pill — horizontal, rx 4, text centered. -->
        <rect x={spotX - spotChipW / 2} y={spotChipY}
              width={spotChipW} height={spotChipH} rx="4"
              fill="#22d3ee" stroke="#0891b2" stroke-width="1"/>
        <text x={spotX} y={spotChipY + spotChipH / 2 + 4}
              text-anchor="middle"
              fill="#0c1830"
              font-size="12" font-weight="800"
              style="font-family: var(--font-numeric); font-variant-numeric: tabular-nums">
          {spotLabel}
        </text>
      {/if}

      <!-- Hover crosshair — vertical line only; SPOT/TDAY/EXP values
           are rendered in the HTML .chart-tooltip overlay below,
           matching ChartWorkspace's popup approach so both chart
           surfaces share the same canonical styling. -->
      {#if hover}
        <line x1={hover?.x} x2={hover?.x} y1={PAD_T} y2={height - PAD_B}
              stroke="rgba(255,255,255,0.20)" stroke-width="1"/>
      {/if}
    </svg>
    <!-- Foreground SVG — just the curves + the live spot dot,
         redrawn ON TOP of the .payoff-stats overlay so the curve
         reads through the panel while text labels (breakeven
         chips, σ ticks, price values) stay underneath. Same
         viewBox / dimensions as the bg SVG so coords align. No
         event handlers + pointer-events:none so hover / zoom /
         pan stay on the bg SVG; this layer is purely visual. -->
    <svg viewBox="0 0 {W} {height}" preserveAspectRatio="none"
         class="payoff-svg-fg"
         aria-hidden="true"
         pointer-events="none">
      {#each intermediatePaths as ip (ip.elapsed)}
        <path d={ip.d} fill="none" stroke={ip.color}
              stroke-width="1" stroke-dasharray="2 2"
              stroke-opacity="0.65" class="data-path"/>
      {/each}
      <path d={pathExpiry} fill="none" stroke="#7dd3fc"
            stroke-width="1.25" stroke-dasharray="4 3" stroke-opacity="0.85"
            class="data-path"/>
      <path d={pathToday}  fill="none" stroke="#fbbf24" stroke-width="1.75"
            class="data-path"/>
      <!-- Foreground spot × today-curve dart — amber (today curve hue).
           Re-painted on the fg layer so the marker sits cleanly on
           top of the curves regardless of paint order. -->
      {#if currentPnl != null && spot >= sMin && spot <= sMax}
        <circle cx={spotX} cy={yOf(currentPnl)} r="6"
                fill="none" stroke="#fbbf24" stroke-width="1.25"
                stroke-opacity="0.55"
                pointer-events="none"/>
        <circle cx={spotX} cy={yOf(currentPnl)} r="3"
                fill="#fbbf24" fill-opacity="0.85"
                stroke="#0c1830" stroke-width="1"
                pointer-events="none"/>
      {/if}
      <!-- Foreground spot × expiry-curve dart — sky-blue (expiry hue). -->
      {#if curveAtSpot != null && spot >= sMin && spot <= sMax}
        <circle cx={spotX} cy={yOf(curveAtSpot.expiry_value)} r="6"
                fill="none" stroke="#7dd3fc" stroke-width="1.25"
                stroke-opacity="0.55"
                pointer-events="none"/>
        <circle cx={spotX} cy={yOf(curveAtSpot.expiry_value)} r="3"
                fill="#7dd3fc" fill-opacity="0.85"
                stroke="#0c1830" stroke-width="1"
                pointer-events="none"/>
      {/if}
    </svg>
    <!-- HTML hover tooltip overlay — SPOT / TDAY / EXP values at hover
         spot. Positioned absolutely inside .payoff-svg-stack (position:
         relative) using percentage coordinates derived from the viewBox
         dimensions so the box tracks the crosshair across viewport widths.
         Click-anywhere-to-close preserved: pointer-events:auto on the div
         itself; stopPropagation on pointerdown so the SVG pan handler
         never fires when tapping the popup. -->
    {#if hover}
      {@const _tx = Math.min(W - 165 - PAD_R, Math.max(PAD_L, (hover?.x ?? 0) + 10))}
      {@const _ty = Math.max(PAD_T, (hover?.y ?? 0) - 58)}
      <div class="chart-tooltip chart-tooltip-pinned payoff-hover-tooltip"
           style="left: {(_tx / W) * 100}%; top: {(_ty / height) * 100}%;"
           role="button" tabindex="0"
           aria-label="Close tooltip — press anywhere"
           onclick={(e) => { e.stopPropagation(); _dismissHover(); }}
           onpointerdown={(e) => { e.stopPropagation(); _dismissHover(); }}
           onkeydown={(e) => {
             if (e.key === 'Enter' || e.key === ' ' || e.key === 'Escape') {
               e.preventDefault();
               _dismissHover();
             }
           }}>
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-label">SPOT</span>
          <span class="chart-tooltip-value payoff-tt-spot">{fmtSpot(hover?.spot)}</span>
        </div>
        {#if hover?.today != null}
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-label">TODAY</span>
          <span class="chart-tooltip-value" class:up={(hover?.today ?? 0) >= 0} class:down={(hover?.today ?? 0) < 0}>
            {fmtMoney(hover?.today)}
          </span>
        </div>
        {/if}
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-label">EXP</span>
          <span class="chart-tooltip-value" class:up={(hover?.expiry ?? 0) >= 0} class:down={(hover?.expiry ?? 0) < 0}>
            {fmtMoney(hover?.expiry)}
          </span>
        </div>
      </div>
    {/if}
    </div>
    <div class="payoff-legend">
      <!-- Legend leg-label list retired per operator request — the
           per-leg breakdown is already in the Candidates panel below
           the chart; surfacing the same list inside the chart card
           was duplication. The Today / Expiry / BE / Spot key items
           below are the only legend pieces the chart needs. -->
      <span class="legend-item">
        <span class="legend-line legend-today"></span>
        Today
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
        Expiry
      </span>
      <span class="legend-item legend-be">
        <span class="legend-mark legend-be-mark"></span>
        Breakeven
      </span>
      <span class="legend-item">
        <span class="legend-mark legend-spot-mark"></span>
        Spot
      </span>
      <!-- Spot anchor chip / rolls-in-N-days chip removed from the
           legend per operator request. The anchor contract is still
           surfaced in the stat-overlay (top-left) `anchor` line for
           operators who need to see which contract is driving the
           spot proxy; the legend-side duplicate was noise next to
           the SPOT readout. -->
      {#if onToggleHoldings && includeHoldings !== null}
        <!-- Holdings toggle — compact press-button (slice AX). Default
             OFF. Active state: cyan filled chip. Inactive: bare dim
             outline. Saves ~15px of legend real estate vs the prior
             slider-style. Operator: "make hold as similar to a button
             to press on and off. by default it is off. this will make
             it reduce less space in payoff". -->
        <button type="button" class="legend-toggle"
                class:legend-toggle-on={includeHoldings}
                aria-pressed={includeHoldings}
                title={includeHoldings
                  ? 'Holdings ON — equity holdings included in Legs + overlaid on payoff. Click to hide.'
                  : 'Holdings OFF — equity holdings hidden. Click to include.'}
                onclick={() => onToggleHoldings && onToggleHoldings()}>HOLD</button>
      {/if}
    </div>
  {#if loading}
    <div class="payoff-loading-ring" aria-label="Loading payoff…" role="status">
      <svg class="payoff-spin-svg" width="22" height="22" viewBox="0 0 16 16"
           fill="none" stroke="currentColor" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round"
           aria-hidden="true">
        <path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.9" />
        <path d="M13.5 3v3h-3" />
      </svg>
    </div>
  {/if}
  {/if}
</div>

<style>
  .payoff-chart {
    background: var(--card-bg-gradient);
    border: 1px solid rgba(251,191,36,0.18);
    border-radius: 4px;
    padding: 6px 8px 8px;
    width: 100%;
    box-sizing: border-box;
    position: relative;
  }
  /* Stack container for the two SVG layers. Both SVGs use
     position:absolute; inset:0 so they ALWAYS occupy the same
     box regardless of reflow timing — critical for iOS Safari
     orientation changes, where the prior top/left/right pinning
     of the fg SVG could race against the bg SVG's width:100%
     and end up with mismatched effective widths after rotation.
     Pinned height via --chart-h makes the wrapper deterministic
     so the viewBox maps the same way every frame. */
  .payoff-svg-stack {
    position: relative;
    width: 100%;
    height: var(--chart-h, 280px);
  }
  /* Fullscreen card → chart fills the viewport. The parent card has
     `.fs-card-on` (position: fixed; inset: 2rem); take everything
     minus the card header (~5rem) + a comfortable bottom pad for
     stats + legend. */
  :global(.fs-card-on) .payoff-svg-stack {
    height: calc(100vh - 12rem) !important;
    min-height: 360px;
  }
  /* Scale the stat overlay to match the magnified chart so the
     numerics stay legible at viewport size. Same ratio the chart
     itself grows by — roughly 1.6× the inline font sizes. */
  :global(.fs-card-on) .payoff-stats {
    font-size: var(--fs-xl);
    padding: 0.55rem 0.85rem;
    column-gap: 0.7rem;
    row-gap: 0.15rem;
    top: 0.7rem;
    left: 0.9rem;
  }
  :global(.fs-card-on) .ps-k     { font-size: 14px; }
  :global(.fs-card-on) .ps-k-hint { font-size: 12px; }
  @media (max-width: 600px) {
    :global(.fs-card-on) .payoff-svg-stack {
      height: calc(100vh - 9rem) !important;
    }
    :global(.fs-card-on) .payoff-stats { font-size: var(--fs-lg); }
    :global(.fs-card-on) .ps-k         { font-size: 11px; }
    :global(.fs-card-on) .ps-k-hint    { font-size: 10px; }
  }
  .payoff-svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
    cursor: crosshair;
    touch-action: pan-y;
    /* Background layer — text labels, axis ticks, breakeven chips,
       grid. Sits BELOW the .payoff-stats overlay so the overlay
       covers any text underneath when they collide. */
    z-index: 1;
  }
  .payoff-svg.payoff-panning { cursor: grabbing; }
  /* Foreground layer — curves only, redrawn on top of the
     overlay so the chart line stays readable across the stats
     panel. Same absolute box as the bg SVG via inset:0. */
  .payoff-svg-fg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
    pointer-events: none;
    z-index: 4;
  }
  .payoff-reset {
    position: absolute;
    top: 0.4rem;
    /* Sit to the LEFT of the 1.4rem icon RefreshButton at right: 0.6rem
       (0.6 + 1.4 + 0.3 gap = 2.3 rem). */
    right: 2.3rem;
    font-family: monospace;
    font-size: var(--fs-md);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 1px 6px;
    border-radius: 2px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
    cursor: pointer;
    /* SVG has z-index: 2 — reset must sit above it too. */
    z-index: 5;
  }
  .payoff-reset:hover {
    background: rgba(251,191,36,0.20);
    border-color: rgba(251,191,36,0.65);
  }

  .payoff-empty {
    height: var(--chart-h, 280px);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-muted);
    font-size: var(--fs-md);
    font-family: monospace;
  }
  .payoff-legend {
    display: flex;
    gap: 0.9rem;
    flex-wrap: wrap;
    align-items: center;
    /* Snug against the SVG bottom — the x-axis labels inside SVG
       provide enough breathing room above the legend. The earlier
       0.3rem margin + 0.4rem padding + border combination left a
       visible empty band between the labels and the legend. */
    padding-top: 0.15rem;
    font-size: var(--fs-md);
    font-family: monospace;
    color: var(--algo-slate);
    margin-top: 0;
  }
  /* Holdings toggle — right-anchored slider-style switch. Track is a
     12-wide pill; thumb is a 7-tall circle that slides between left
     (OFF) and right (ON). Sky-cyan palette matches the legend's
     existing accent. `margin-left: auto` pushes it to the far right
     so it never crowds the legend keys. */
  .legend-toggle {
    margin-left: auto;
    padding: 0.1rem 0.35rem;
    border-radius: 3px;
    background: transparent;
    border: 1px solid rgba(126, 151, 184, 0.35);
    color: var(--algo-muted);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    cursor: pointer;
    user-select: none;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .legend-toggle:hover {
    color: #cfe3f8;
    border-color: rgba(126, 151, 184, 0.6);
  }
  .legend-toggle-on {
    background: rgba(125, 211, 252, 0.16);
    border-color: rgba(125, 211, 252, 0.65);
    color: #7dd3fc;
  }
  .legend-toggle-on:hover {
    background: rgba(125, 211, 252, 0.22);
  }
  .legend-legs {
    display: inline-flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.5rem;
    font-size: var(--fs-sm);
  }
  .legend-sep {
    display: inline-block;
    width: 1px;
    height: 0.8em;
    background: rgba(200, 216, 240, 0.2);
    align-self: center;
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
  .legend-today  { border-top: 2px solid var(--c-action); }
  .legend-expiry { border-top: 1.5px dashed #7dd3fc; }
  /* Intermediate-DTE swatch — the stroke colour comes inline via
     `style="border-top-color: …"` because each slice gets its own
     interpolated hsl(). Dashed, matching the chart curve. */
  .legend-mid    { border-top: 1.5px dashed var(--c-action); }
  .legend-mark {
    display: inline-block;
    width: 0;
    height: 12px;
  }
  /* O1: legend spot mark thinned to match the subtle 1px chart line */
  .legend-spot-mark { border-left: 1px solid rgba(34,211,238,0.4); }
  .legend-be-mark   { border-left: 2px dashed #fde68a; }
  /* Solid swatch for the spot legend — visually pairs with the
     SOLID cyan vertical drawn at the spot price (BE is dashed
     cream, σ ticks are dashed amber/light-blue). */

  /* On-chart stat overlay — reads the key numerics off the curve so
     the chart is self-contained. Sits top-left, semi-transparent,
     pointer-events disabled so it never blocks the SVG hover/zoom.
     O4: flat appearance — solid navy bg, thin sky-blue border, no
     shadow, no gradient. */
  .payoff-stats {
    position: absolute;
    top: 0.5rem;
    left: 0.6rem;
    display: grid;
    grid-template-columns: max-content max-content;
    column-gap: 0.45rem;
    row-gap: 0.08rem;
    padding: 0.26rem 0.48rem;
    border-radius: 6px;
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(125,211,252,0.20);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    line-height: 1.25;
    /* Sits BETWEEN the bg SVG (z=1, text labels) and the fg SVG
       (z=4, curves). Text labels behind the overlay are covered
       by the stats panel; the curve gets redrawn over the panel
       by the fg SVG so the chart line reads clean across. */
    z-index: 3;
  }
  .ps-row {
    display: contents;
    cursor: help;
  }
  .ps-k {
    /* Amber label tier — bumped to 0.6rem (was 9px literal) so the
       overlay text is at the ~10px legibility floor on default DPI. */
    color: var(--c-action);
    letter-spacing: 0.08em;
    font-size: var(--fs-sm);
    font-weight: 700;
    opacity: 0.85;
    align-self: center;
  }
  /* Inline hint after the σ glyph — small "IV" tag clarifies what
     the symbol means without giving up the canonical σ shorthand. */
  .ps-k-hint {
    margin-left: 0.2rem;
    font-size: 9px;
    font-weight: 500;
    color: #fde68a;
    opacity: 0.75;
    letter-spacing: 0.06em;
  }
  .ps-v {
    text-align: right;
    font-weight: 700;
    font-size: 10px;
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
  }
  .ps-v.ps-spot { color: #7dd3fc; }
  /* Day-direction tint on the SPOT readout — green when above
     yesterday's close, red below. Falls through to the neutral
     cyan (`ps-spot-flat`) when prev_close is unavailable. */
  .ps-v.ps-spot-pos  { color: var(--c-long); }
  .ps-v.ps-spot-neg  { color: var(--c-short); }
  .ps-v.ps-spot-flat { color: #7dd3fc; }
  .ps-v.ps-pos  { color: var(--c-long); }
  .ps-v.ps-neg  { color: var(--c-short); }
  /* PREV row sits between SPOT and TDAY; neutral cyan so the eye
     reads SPOT (directional colour) → PREV (anchor) without
     re-tinting itself. */
  .ps-v.ps-flat { color: var(--algo-slate); }
  .payoff-multi-expiry-note {
    margin: 0.25rem 0 0;
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    font-style: italic;
    /* Contrast raised 0.55 → 0.85 alpha — earlier rendered ~2.5:1
       against the navy chart bg, below the 4.5:1 target for body
       text. Operator's only multi-expiry context cue. */
    color: rgba(200, 216, 240, 0.85);
  }

  /* Spot-anchor chip — slate-blue palette for normal state;
     flips to amber when the contract expires within 3 days. */
  .payoff-anchor-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    margin: 0.25rem 0 0;
    padding: 1px 6px;
    font-size: var(--fs-xs);
    font-family: var(--font-numeric);
    color: var(--algo-muted);
    background: rgba(125, 145, 184, 0.08);
    border: 1px solid rgba(125, 145, 184, 0.22);
    border-radius: 3px;
    cursor: default;
  }
  /* In-line variant — sits next to the "Spot" legend label rather
     than below the legend row. Tighter padding + zero top margin so
     the chip reads as a trailing tag on the Spot item, not a
     standalone block. */
  .payoff-anchor-chip--inline {
    margin: 0 0 0 0.35rem;
    padding: 0 5px;
    font-size: var(--fs-xs);
    line-height: 1.4;
  }
  /* Amber roll-warning state */
  .payoff-anchor-chip--amber {
    color: var(--c-action);
    background: var(--algo-amber-bg);
    border-color: rgba(251, 191, 36, 0.42);
  }
  /* ── Payoff chart hover tooltip local overrides ───────────────────────
     Canonical shell + colors come from app.css (.chart-tooltip family).
     Local additions: z-index above the fg SVG layer (z:4), pointer
     cursor, and min-width to accommodate the wide SPOT/TDAY/EXP labels.
     The .payoff-tt-spot sky-cyan color overrides the default slate value
     so SPOT reads as a reference/info value, matching the stat overlay. */
  .payoff-hover-tooltip {
    cursor: pointer;
    z-index: 6;
    min-width: 10rem;
    max-width: 14rem;
  }
  .payoff-tt-spot {
    color: #7dd3fc;
  }
  .payoff-loading-ring {
    position: absolute;
    top: 0.4rem;
    right: 0.5rem;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
    z-index: 2;
  }
  .payoff-spin-svg {
    color: var(--c-info, #22d3ee);
    animation: payoff-spin 1.1s linear infinite;
    transform-origin: 50% 50%;
    opacity: 0.75;
  }
  @keyframes payoff-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  @media (prefers-reduced-motion: reduce) {
    .payoff-spin-svg { animation: none; }
  }
</style>
