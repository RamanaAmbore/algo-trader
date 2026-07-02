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

  const _pulse = createChartRefreshPulse();

  async function load() {
    loading = true;
    _error = null;
    try {
      const data = await fetchNavHistory({ days: lookback });
      history = Array.isArray(data?.rows) ? data.rows : [];
      if (history.length) _pulse.notify('nav');
    } catch (err) {
      // Capture the error so the operator sees it rather than a
      // silent empty state. 401 on demo/anon: the message will read
      // "Unauthorized" which is accurate and actionable.
      _error = (err && typeof err === 'object' && 'message' in err)
        ? String(/** @type {any} */ (err).message).slice(0, 80)
        : 'Failed to load NAV history';
    } finally {
      loading = false;
    }
  }

  function _retry() {
    _error = null;
    load();
  }

  // Polls on the market-aware interval — same cadence the NavCard
  // headline uses. NAV doesn't tick frequently so this is cheap.
  let _stop = () => {};
  onMount(() => {
    load();
    // 60s — NAV doesn't tick frequently; the headline NavCard polls
    // on its own faster cadence, this chart just trails the daily
    // snapshot landing at 16:00 IST + manual recomputes.
    // Throttle to 60 s on hidden — NAV data doesn't change frequently;
    // keeping a slow heartbeat ensures the chart is fresh on tab return.
    _stop = marketAwareInterval(load, 60_000, 60_000);
  });
  onDestroy(() => { _stop(); });

  function _fmtInr(/** @type {number} */ n) {
    if (n == null || !isFinite(n)) return '—';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency', currency: 'INR', maximumFractionDigits: 0,
    }).format(n);
  }
  function _fmtChipInr(/** @type {number|null|undefined} */ v) {
    if (v == null || !isFinite(v)) return '—';
    if (Math.abs(v) >= 10000000) return `₹${(v/10000000).toFixed(2)}Cr`;
    if (Math.abs(v) >= 100000)   return `₹${(v/100000).toFixed(2)}L`;
    if (Math.abs(v) >= 1000)     return `₹${(v/1000).toFixed(1)}k`;
    return `₹${Math.round(Number(v))}`;
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
          {chipDeltaPct >= 0 ? '+' : ''}{(chipDeltaPct * 100).toFixed(2)}%
        </span>
      {/if}
    </div>
  {/if}

  {#if history.length >= 2}
    {@const _pad = { l: 60, r: 12, t: 12, b: 24 }}
    {@const W = 760}
    {@const H = 260}
    {@const innerW = W - _pad.l - _pad.r}
    {@const innerH = H - _pad.t - _pad.b}
    {@const _navs = history.map(p => p.nav)}
    {@const _min = Math.min(..._navs)}
    {@const _max = Math.max(..._navs)}
    {@const _range = (_max - _min) || Math.max(Math.abs(_max), 1)}
    {@const yOf = (v) => _pad.t + innerH - ((v - _min) / _range) * innerH}
    {@const xOf = (i) => _pad.l + (history.length === 1 ? innerW / 2 : (i * innerW) / (history.length - 1))}
    {@const path = history.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i)} ${yOf(p.nav)}`).join(' ')}
    <div class="nav-tab-meta">{history.length} days</div>
    <svg class="nav-svg" viewBox="0 0 760 260" preserveAspectRatio="none"
         aria-label="Firm NAV history">
      {#each [0.0, 0.25, 0.5, 0.75, 1.0] as t}
        {@const y = _pad.t + innerH * t}
        {@const v = _max - _range * t}
        <line class="chart-grid-line" x1={_pad.l} y1={y} x2={_pad.l + innerW} y2={y} />
        <text class="nav-yaxis-label" x={_pad.l - 8} y={y + 3} text-anchor="end"
              fill="rgba(155,176,208,0.55)" font-size="10"
              style="font-family: var(--font-numeric)">{_fmtInr(v)}</text>
      {/each}
      <path d={path} fill="none" stroke="#fbbf24" stroke-width="2" class="data-path"/>
      <circle cx={xOf(history.length - 1)} cy={yOf(_navs[_navs.length - 1])}
              r="3" fill="#fbbf24" stroke="#0a1020" stroke-width="1" />
      <text x={xOf(0)} y={H - 6} text-anchor="start"
            fill="rgba(155,176,208,0.55)" font-size="10"
            style="font-family: var(--font-numeric)">{history[0].as_of_date}</text>
      <text x={xOf(history.length - 1)} y={H - 6} text-anchor="end"
            fill="rgba(155,176,208,0.55)" font-size="10"
            style="font-family: var(--font-numeric)">{history[history.length - 1].as_of_date}</text>
    </svg>
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
      No NAV snapshots yet. First snapshot lands at 16:00 IST.
    </div>
  {/if}
</div>

<style>
  /* Wrapper is the positioning context for the overlay chip. */
  .nav-tab-wrap {
    position: relative;
    width: 100%;
  }
  .nav-tab-meta {
    font-size: var(--fs-xs);
    color: rgba(155, 176, 208, 0.55);
    text-align: right;
    padding-right: 0.4rem;
  }
  .nav-svg {
    display: block;
    width: 100%;
    height: auto;
    aspect-ratio: 760 / 260;
    background: rgba(15, 23, 42, 0.25);
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
    color: #f87171;
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
    background: rgba(34, 211, 238, 0.14);
    color: #22d3ee;
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: background 120ms, border-color 120ms;
  }
  .nav-tab-retry:hover {
    background: rgba(34, 211, 238, 0.22);
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
    color: #7e97b8;
  }
  .nav-chip-val {
    font-size: var(--fs-lg);
    font-weight: 800;
    color: #67e8f9;
  }
  .nav-chip-delta {
    font-size: var(--fs-md);
    font-weight: 700;
    color: #c8d8f0;
  }
  .nav-chip-overlay.nav-chip-pos {
    background: rgba(74, 222, 128, 0.10);
    border-color: rgba(74, 222, 128, 0.40);
  }
  .nav-chip-overlay.nav-chip-pos .nav-chip-val,
  .nav-chip-overlay.nav-chip-pos .nav-chip-delta { color: #4ade80; }
  .nav-chip-overlay.nav-chip-neg {
    background: rgba(248, 113, 113, 0.10);
    border-color: rgba(248, 113, 113, 0.40);
  }
  .nav-chip-overlay.nav-chip-neg .nav-chip-val,
  .nav-chip-overlay.nav-chip-neg .nav-chip-delta { color: #f87171; }
</style>
