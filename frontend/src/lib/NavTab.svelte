<!--
  NavTab — firm NAV curve chart. Single tab on the dashboard's
  equity-curve card (alongside Intraday and Performance).

  Replaces the standalone /nav page (deleted). Only the chart was
  worth keeping — the per-account breakdown lives on /performance,
  the headline number on NavCard, and the daily snapshot table was
  rarely used. Operator: "not entire nav page. just move the nav
  chart and delete the current nav page."

  NAV chip overlay (Jun 2026): the firm-NAV chip (last-computed
  firm NAV + day delta) renders as an absolutely-positioned overlay
  at the top-LEFT of the chart. Replaces the prior dedicated
  `.dash-nav-row` row on the dashboard — operator: "move nav chip
  as an overlay in nav chart in dashboard" and later "move nav
  chip to the left of nav chart". Chip is read-only here (no
  click handler — we're already inside the NAV tab); the parent
  owns the data fetch and passes it in via props.
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { fetchNavHistory } from '$lib/api';
  import { marketAwareInterval } from '$lib/stores';
  import { createChartRefreshPulse } from '$lib/data/chartRefreshPulse.svelte.js';
  import { fmtPctFraction } from '$lib/format';
  import ChartCrosshair from '$lib/ChartCrosshair.svelte';
  import ChartPopup from '$lib/ChartPopup.svelte';
  import { priceFmt } from '$lib/format';

  // SVG viewBox geometry — shared by the template and the hover handler.
  const NAV_W = 760;
  const NAV_H = 260;
  const NAV_PAD = { l: 60, r: 12, t: 12, b: 24 };

  /**
   * @typedef {Object} Props
   * @property {{nav:number, as_of_date:string}|null} [chipLatest]
   *   Last-computed firm NAV (for the overlay chip). null hides the chip.
   * @property {number|null} [chipDelta]
   *   Day Δ in INR — drives green/red tint on the chip.
   * @property {number|null} [chipDeltaPct]
   *   Day Δ in pct (fraction, e.g. 0.012 for +1.2%) — rendered as the
   *   second line of the chip when present.
   */
  /** @type {Props} */
  let {
    chipLatest   = null,
    chipDelta    = null,
    chipDeltaPct = null,
  } = $props();

  /** @typedef {{ as_of_date: string, nav: number }} NavPoint */
  /** @type {NavPoint[]} */
  let history = $state([]);
  let lookback = $state(90);
  let loading = $state(false);
  /** @type {string|null} */
  let _error = $state(null);
  /** Index into `history` under the pointer, null when not hovering. */
  /** @type {number|null} */
  let hovIdx = $state(null);

  /** @param {PointerEvent} e */
  function _onPointerMove(e) {
    if (history.length < 2) return;
    const rect = /** @type {SVGElement} */ (e.currentTarget).getBoundingClientRect();
    // preserveAspectRatio="none": viewBox x maps linearly across the full width.
    const vbX = ((e.clientX - rect.left) / rect.width) * NAV_W;
    const innerW = NAV_W - NAV_PAD.l - NAV_PAD.r;
    const frac = Math.max(0, Math.min(1, (vbX - NAV_PAD.l) / innerW));
    hovIdx = Math.round(frac * (history.length - 1));
  }
  function _onPointerLeave() { hovIdx = null; }

  const _pulse = createChartRefreshPulse();

  // Per-call AbortController so a network stall (mobile, broker TLS
  // handshake, backend slow query) never pins `loading = true`
  // indefinitely. Cancelled on component unmount and on every new
  // load() invocation (one in-flight at a time).
  /** @type {AbortController|null} */
  let _ac = null;

  async function load() {
    // Cancel any prior in-flight request before starting a new one.
    if (_ac) { _ac.abort(); }
    _ac = new AbortController();
    const { signal } = _ac;

    // 15 s hard timeout — covers slow mobile + stalled backend.
    const _timeout = setTimeout(() => _ac?.abort(), 15_000);

    loading = true;
    _error = null;
    try {
      const data = await fetchNavHistory({ days: lookback, signal });
      history = Array.isArray(data?.rows) ? data.rows : [];
      if (history.length) _pulse.notify('nav');
    } catch (err) {
      // AbortError: either the 15s timeout fired, a superseding load()
      // replaced and aborted us, or onDestroy unmounted the component.
      // Show the retry banner ONLY when THIS call's signal is still
      // current. If a newer load() has replaced _ac, or onDestroy nulled
      // it, stay silent — the replacement will resolve on its own.
      if (/** @type {any} */ (err)?.name === 'AbortError') {
        if (_ac?.signal === signal) {
          _error = 'NAV history timed out — click Retry';
        }
        return;
      }
      // Non-abort errors (network, 4xx/5xx): write to state only when
      // this call is still current, preventing a stale failure from
      // overwriting a successfully-loaded replacement.
      if (_ac?.signal === signal || !_ac) {
        _error = (err && typeof err === 'object' && 'message' in err)
          ? String(/** @type {any} */ (err).message).slice(0, 80)
          : 'Failed to load NAV history';
      }
    } finally {
      clearTimeout(_timeout);
      // Only flip terminal state (loading=false, _ac=null) when THIS
      // call is still the current one. A newer load() that aborted us
      // is now managing its own state; clearing loading here would
      // prematurely remove the spinner for the in-flight replacement.
      if (_ac?.signal === signal) {
        loading = false;
        _ac = null;
      }
    }
  }

  function _retry() {
    _error = null;
    load();
  }

  // Polls on the market-aware interval — cheap, since this curve is a
  // genuinely once-per-trading-day series (see below), not a live feed.
  let _stop = () => {};
  onMount(() => {
    load();
    // 60s — this chart plots `nav_daily`, one row per trading day,
    // written at the MCX close-settled moment (≈23:45 IST — MCX close
    // 23:30 + 15 min settlement offset; 2026-09 fix, previously an
    // inaccurate fixed 16:00 IST that predated MCX's own close). The
    // 60s poll here just picks that single daily write up promptly +
    // reflects any operator-triggered manual recompute; it does not
    // make the curve itself "live" — see the EOD label in the markup
    // below. The overlay CHIP (chipLatest prop, top-left) is a
    // DIFFERENT, genuinely live figure — see the dashboard's
    // `_fetchNav()` (polls GET /api/auth/firm-nav every 60s).
    _stop = marketAwareInterval(load, 60_000, 60_000);
  });
  onDestroy(() => {
    _stop();
    // Abort any in-flight fetch to avoid "state updated after unmount"
    // and to release the network connection.
    if (_ac) { _ac.abort(); _ac = null; }
  });

  function _fmtInr(/** @type {number} */ n) {
    if (n == null || !isFinite(n)) return '—';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency', currency: 'INR', maximumFractionDigits: 0,
    }).format(n);
  }
  function _fmtChipInr(/** @type {number|null|undefined} */ v) {
    if (v == null || !isFinite(v)) return '—';
    if (Math.abs(v) >= 10000000) return `${(v/10000000).toFixed(2)}Cr`;
    if (Math.abs(v) >= 100000)   return `${(v/100000).toFixed(2)}L`;
    if (Math.abs(v) >= 1000)     return `${(v/1000).toFixed(1)}k`;
    return `${Math.round(Number(v))}`;
  }
</script>

<div class="nav-tab-wrap {_pulse.classOf('nav')}">
  <!-- NAV chip overlay — top-LEFT of the chart. Self-hides when
       chipLatest is null (operator lacks view_nav cap or no
       snapshot has landed yet). Read-only inside the NAV tab — the
       operator is already viewing the curve, so there's nothing to
       navigate to. The cyan-rest + green/red day-Δ tint stays
       consistent with the prior dedicated chip row. -->
  {#if chipLatest}
    <div class="nav-chip-overlay"
         class:nav-chip-pos={(chipDelta ?? 0) > 0}
         class:nav-chip-neg={(chipDelta ?? 0) < 0}
         title={`NAV ${_fmtChipInr(chipLatest.nav)} as of ${chipLatest.as_of_date}`}>
      <span class="nav-chip-lbl">NAV</span>
      <span class="nav-chip-val">{_fmtChipInr(chipLatest.nav)}</span>
      {#if chipDeltaPct != null}
        <span class="nav-chip-delta">
          {fmtPctFraction(chipDeltaPct, 2, true)}
        </span>
      {/if}
    </div>
  {/if}

  {#if history.length >= 2}
    {@const _pad = NAV_PAD}
    {@const W = NAV_W}
    {@const H = NAV_H}
    {@const innerW = W - _pad.l - _pad.r}
    {@const innerH = H - _pad.t - _pad.b}
    {@const _navs = history.map(p => p.nav)}
    {@const _min = Math.min(..._navs)}
    {@const _max = Math.max(..._navs)}
    {@const _range = (_max - _min) || Math.max(Math.abs(_max), 1)}
    {@const yOf = (v) => _pad.t + innerH - ((v - _min) / _range) * innerH}
    {@const xOf = (i) => _pad.l + (history.length === 1 ? innerW / 2 : (i * innerW) / (history.length - 1))}
    {@const path = history.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i)} ${yOf(p.nav)}`).join(' ')}
    {@const _hovX = hovIdx != null && hovIdx < history.length ? xOf(hovIdx) : null}
    <div class="nav-tab-meta">
      {history.length} days
      <span class="nav-tab-eod-badge"
            title="One point per trading day, written at end-of-day settlement — not a live-updating curve.">EOD</span>
    </div>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <!-- Pointer handlers only drive the hover crosshair; the aria-label
         already names the chart, and there is no keyboard action to expose. -->
    <div class="cp-frame">
    <svg class="nav-svg" viewBox="0 0 {NAV_W} {NAV_H}" preserveAspectRatio="none"
         aria-label="Firm NAV history (end-of-day, one point per trading day)"
         onpointermove={_onPointerMove}
         onpointerleave={_onPointerLeave}>
      {#each [0.0, 0.25, 0.5, 0.75, 1.0] as t}
        {@const y = _pad.t + innerH * t}
        {@const v = _max - _range * t}
        <line class="chart-grid-line" x1={_pad.l} y1={y} x2={_pad.l + innerW} y2={y} />
        <text class="chart-axis-label nav-yaxis-label" x={_pad.l - 8} y={y + 3} text-anchor="end"
              transform="rotate(-45 {_pad.l - 8} {y + 3})"
              style="font-family: var(--font-numeric)">{_fmtChipInr(v)}</text>
      {/each}
      <path d={path} fill="none" stroke="#fbbf24" stroke-width="2" class="data-path"/>
      <circle cx={xOf(history.length - 1)} cy={yOf(_navs[_navs.length - 1])}
              r="3" fill="#fbbf24" stroke="#0a1020" stroke-width="1" />
      {#if _hovX != null}
        <!-- showDot={false}: the last-point circle above is the data marker;
             the hovered point is read off the vertical line alone. -->
        <ChartCrosshair
          x={_hovX}
          bounds={{ top: _pad.t, bottom: H - _pad.b, left: _pad.l, right: W - _pad.r }}
          mode="vertical"
          showDot={false}
        />
      {/if}
      <text x={xOf(0)} y={H - 6} text-anchor="start"
            fill="var(--algo-muted)" font-size="10"
            style="font-family: var(--font-numeric)">{history[0].as_of_date}</text>
      <text x={xOf(history.length - 1)} y={H - 6} text-anchor="end"
            fill="var(--algo-muted)" font-size="10"
            style="font-family: var(--font-numeric)">{history[history.length - 1].as_of_date}</text>
    </svg>
    {#if hovIdx != null && hovIdx < history.length}
      <ChartPopup x={_hovX} y={yOf(_navs[hovIdx])} viewW={NAV_W} viewH={NAV_H}>
        <div class="chart-tooltip-ts">{history[hovIdx].as_of_date}</div>
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-label">NAV</span>
          <span class="chart-tooltip-value">₹{priceFmt(_navs[hovIdx])}</span>
        </div>
      </ChartPopup>
    {/if}
    </div>
  {:else if _error}
    <!-- Error state — surface message + Retry so the operator can act. -->
    <div class="nav-tab-empty nav-tab-error" role="alert" data-testid="nav-tab-error">
      <span class="nav-tab-err-icon" aria-hidden="true">⚠</span>
      <span class="nav-tab-err-text">NAV history unavailable — {_error}</span>
      <button class="nav-tab-retry" onclick={_retry}>Retry</button>
    </div>
  {:else if loading}
    <div class="nav-tab-empty" data-testid="nav-tab-loading">Loading NAV history…</div>
  {:else}
    <div class="nav-tab-empty" data-testid="nav-tab-empty">
      No NAV snapshots yet. First snapshot lands at end-of-day settlement (≈23:45 IST).
    </div>
  {/if}
</div>

<style>
  /* Wrapper is the positioning context for the overlay chip (position:
     relative anchors .nav-chip-overlay's `left: calc(7.9% + 0.4rem)`) and
     supplies this tab's own content inset (sibling Intraday/Performance
     tabs get theirs from .eq-legend / PnlAnalysis internals, not from a
     shared .card-body padding). The border + background "frame" that used
     to live here was removed (2026-10 card-chrome audit) — it was the ONLY
     one of the three chart-card tabs with its own card chrome, so flipping
     tabs visibly jumped the card's border/radius. Keep position/padding;
     do not reintroduce border/background here. */
  .nav-tab-wrap {
    position: relative;
    width: 100%;
    padding: 6px 8px 8px;
    box-sizing: border-box;
  }
  .nav-tab-meta {
    font-size: var(--fs-xs);
    color: rgba(155, 176, 208, 0.55);
    text-align: right;
    padding-right: 0.4rem;
  }
  /* EOD badge — marks the curve as once-per-trading-day settlement data,
     distinct from the genuinely live overlay chip (top-left). */
  .nav-tab-eod-badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    font-weight: 700;
    letter-spacing: 0.05em;
    color: var(--algo-slate);
    background: rgba(155, 176, 208, 0.12);
    border: 1px solid rgba(155, 176, 208, 0.25);
    cursor: default;
  }
  .nav-svg {
    display: block;
    width: 100%;
    height: auto;
    aspect-ratio: 760 / 260;
    border-radius: 4px;
  }
  /* Mobile NAV card height bump — at ≤600 px the sidebar card collapses
     so far that the curve has no room to breathe (~113 px tall at 393 px
     wide via the 760/260 aspect ratio). Override aspect-ratio with a
     hard min-height so the chart stays readable on phones. Operator:
     "on mobile increase nav chart card height". */
  @media (max-width: 600px) {
    .nav-svg {
      aspect-ratio: auto;
      min-height: 240px;
      height: 240px;
    }
  }
  .nav-tab-empty {
    padding: 1.4rem 0.8rem;
    text-align: center;
    color: rgba(155, 176, 208, 0.55);
    font-size: var(--fs-lg);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }

  /* Error state — red tint matching PerformancePage .perf-banner-error palette. */
  .nav-tab-error {
    background: rgba(248, 113, 113, 0.07);
    color: var(--c-short);
    border-radius: 4px;
    border: 1px solid rgba(248, 113, 113, 0.25);
    margin: 0.5rem;
    font-size: var(--fs-md);
  }

  .nav-tab-err-icon {
    flex-shrink: 0;
    font-size: 1rem;
  }

  .nav-tab-err-text {
    flex: 1 1 auto;
    min-width: 0;
    text-align: left;
  }

  /* Retry button — cyan-400, matches NavBreakdown .nav-bd-retry and the
     project's canonical action palette. */
  .nav-tab-retry {
    flex-shrink: 0;
    padding: 0.2rem 0.7rem;
    border-radius: 3px;
    border: 1px solid rgba(34, 211, 238, 0.55);
    background: var(--c-info-14);
    color: var(--c-info);
    font-size: var(--fs-lg);
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: background 120ms, border-color 120ms;
  }
  .nav-tab-retry:hover {
    background: var(--c-info-22);
    border-color: rgba(34, 211, 238, 0.80);
    color: #67e8f9;
  }

  /* Overlay chip — anchored top-LEFT INSIDE the chart wrapper.
     Operator placement refinement (Jun 2026): "move nav chip to the
     left of nav chart" — left-anchor reads more naturally beside the
     y-axis labels and never overlaps the trailing data point on the
     right edge of the curve. Operator follow-up (Jun 2026): "the nav
     overlay is overlapping the y label in nav chart. start it just
     right of Y axis" — the SVG uses viewBox 760×260 with pad.l = 60
     so the Y-axis line sits at 60/760 = 7.89 % of container width.
     `left: calc(7.9% + 0.4rem)` lands the chip just inside the plot
     area, clearing the rotated Y-tick labels at every viewport. The
     z-index keeps the chip above the SVG without forming a stacking
     context that traps the meta label. Cyan-rest palette + green/red
     day-Δ tint mirrors the prior dedicated chip row, so operators
     don't have to relearn the visual language. */
  .nav-chip-overlay {
    position: absolute;
    top: clamp(0.25rem, 1vw, 0.5rem);
    left: calc(7.9% + 0.4rem);
    z-index: 2;
    display: inline-flex;
    align-items: baseline;
    gap: 0.4rem;
    padding: 0.18rem 0.55rem;
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(34, 211, 238, 0.35);
    border-radius: 4px;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    pointer-events: none;
    backdrop-filter: blur(2px);
  }
  .nav-chip-lbl {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--c-muted);
  }
  .nav-chip-val {
    font-size: var(--fs-lg);
    font-weight: 800;
    color: #67e8f9;
  }
  .nav-chip-delta {
    font-size: var(--fs-md);
    font-weight: 700;
    color: var(--algo-slate);
  }
  .nav-chip-overlay.nav-chip-pos {
    background: var(--c-long-10);
    border-color: rgba(74, 222, 128, 0.40);
  }
  .nav-chip-overlay.nav-chip-pos .nav-chip-val,
  .nav-chip-overlay.nav-chip-pos .nav-chip-delta { color: var(--c-long); }
  .nav-chip-overlay.nav-chip-neg {
    background: var(--c-short-10);
    border-color: rgba(248, 113, 113, 0.40);
  }
  .nav-chip-overlay.nav-chip-neg .nav-chip-val,
  .nav-chip-overlay.nav-chip-neg .nav-chip-delta { color: var(--c-short); }
</style>
