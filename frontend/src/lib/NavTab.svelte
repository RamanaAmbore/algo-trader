<!--
  NavTab — firm NAV curve chart. Single tab on the dashboard's
  equity-curve card (alongside Intraday and Performance).

  Replaces the standalone /nav page (deleted). Only the chart was
  worth keeping — the per-account breakdown lives on /performance,
  the headline number on NavCard, and the daily snapshot table was
  rarely used. Operator: "not entire nav page. just move the nav
  chart and delete the current nav page."
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { fetchNavHistory } from '$lib/api';
  import { marketAwareInterval } from '$lib/stores';

  /** @typedef {{ as_of_date: string, nav: number }} NavPoint */
  /** @type {NavPoint[]} */
  let history = $state([]);
  let lookback = $state(90);
  let loading = $state(false);

  async function load() {
    loading = true;
    try {
      const data = await fetchNavHistory({ days: lookback });
      history = Array.isArray(data?.rows) ? data.rows : [];
    } catch (_) {
      // Demo / anon hits 401 — leave history empty so the empty
      // state below renders. No toast — this is a passive chart.
    } finally {
      loading = false;
    }
  }

  // Polls on the market-aware interval — same cadence the NavCard
  // headline uses. NAV doesn't tick frequently so this is cheap.
  let _stop = () => {};
  onMount(() => {
    load();
    // 60s — NAV doesn't tick frequently; the headline NavCard polls
    // on its own faster cadence, this chart just trails the daily
    // snapshot landing at 16:00 IST + manual recomputes.
    _stop = marketAwareInterval(load, 60_000);
  });
  onDestroy(() => { _stop(); });

  function _fmtInr(/** @type {number} */ n) {
    if (n == null || !isFinite(n)) return '—';
    return new Intl.NumberFormat('en-IN', {
      style: 'currency', currency: 'INR', maximumFractionDigits: 0,
    }).format(n);
  }
</script>

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
      <line x1={_pad.l} y1={y} x2={_pad.l + innerW} y2={y}
            stroke="rgba(126,151,184,0.10)" stroke-width="1" />
      <text x={_pad.l - 8} y={y + 3} text-anchor="end"
            fill="rgba(155,176,208,0.55)" font-size="10"
            font-family="ui-monospace, monospace">{_fmtInr(v)}</text>
    {/each}
    <path d={path} fill="none" stroke="#fbbf24" stroke-width="2" />
    <circle cx={xOf(history.length - 1)} cy={yOf(_navs[_navs.length - 1])}
            r="3" fill="#fbbf24" stroke="#0a1020" stroke-width="1" />
    <text x={xOf(0)} y={H - 6} text-anchor="start"
          fill="rgba(155,176,208,0.55)" font-size="10"
          font-family="ui-monospace, monospace">{history[0].as_of_date}</text>
    <text x={xOf(history.length - 1)} y={H - 6} text-anchor="end"
          fill="rgba(155,176,208,0.55)" font-size="10"
          font-family="ui-monospace, monospace">{history[history.length - 1].as_of_date}</text>
  </svg>
{:else if loading}
  <div class="nav-tab-empty">Loading NAV history…</div>
{:else}
  <div class="nav-tab-empty">
    No NAV snapshots yet. First snapshot lands at 16:00 IST.
  </div>
{/if}

<style>
  .nav-tab-meta {
    font-size: 0.55rem;
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
  .nav-tab-empty {
    padding: 1.4rem 0.8rem;
    text-align: center;
    color: rgba(155, 176, 208, 0.55);
    font-size: 0.72rem;
  }
</style>
