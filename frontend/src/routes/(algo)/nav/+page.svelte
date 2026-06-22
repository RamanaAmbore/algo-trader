<!--
  /nav — firm-level NAV page.

  Three sections:
    1. Header card — current NAV, day Δ, prior NAV, accounts in
       snapshot.
    2. NAV curve — hand-rolled SVG over the last N days (matches
       /strategies/[id] style for visual consistency).
    3. Composition table — cash + positions MTM + holdings MTM per
       day so the operator can see where the NAV moved.

  Demo / observer read-only. Admin + ops can trigger a recompute
  (button calls POST /api/nav/compute).
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { nowStamp, marketAwareInterval } from '$lib/stores';
  import {
    fetchNavHistory, fetchNavLatest, triggerNavCompute,
    fetchMyNavSlice,
  } from '$lib/api';
  import { authStore } from '$lib/stores';
  import { userRole, userCaps, hasCap } from '$lib/rbac';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';

  /** @typedef {{
   *   as_of_date: string, nav: number, cash_total: number,
   *   positions_mtm: number, holdings_mtm: number,
   *   accounts_snapshot: string[], note: string|null
   * }} NavRow */

  /** @type {NavRow[]} */
  let history = $state([]);
  /** @type {NavRow|null} */
  let latest = $state(null);
  /** @type {NavRow|null} */
  let prior = $state(null);
  let dayDelta    = $state(/** @type {number|null} */ (null));
  let dayDeltaPct = $state(/** @type {number|null} */ (null));
  let loading = $state(false);
  let computing = $state(false);
  let error = $state('');
  let lookback = $state(90);

  /** @typedef {{
   *   username: string, share_pct: number, contribution: number,
   *   firm_nav: number, nav_share: number, pnl: number,
   *   pnl_pct: number|null,
   *   day_delta_share: number|null, day_delta_share_pct: number|null,
   *   as_of_date: string|null
   * }} InvestorSlice */
  /** @type {InvestorSlice|null} */
  let mySlice = $state(null);
  const _signedIn = $derived(!!$authStore?.user);

  const canTrigger = $derived(hasCap('trigger_nav_compute', $userCaps, $userRole));

  async function load() {
    loading = true; error = '';
    try {
      const [list, lat] = await Promise.all([
        fetchNavHistory({ days: lookback }),
        fetchNavLatest(),
      ]);
      history = Array.isArray(list?.rows) ? list.rows : [];
      latest  = lat?.latest ?? null;
      prior   = lat?.prior ?? null;
      dayDelta    = lat?.day_delta ?? null;
      dayDeltaPct = lat?.day_delta_pct ?? null;
      // Investor slice — fetched alongside but tolerated to fail (demo
      // / anon hits 401, no contribution returns zeros). When the
      // user has share_pct == 0 the card hides itself in the markup
      // below.
      if (_signedIn) {
        try {
          mySlice = await fetchMyNavSlice();
        } catch { mySlice = null; }
      } else {
        mySlice = null;
      }
    } catch (e) { error = e?.message || 'NAV fetch failed'; }
    finally { loading = false; }
  }

  async function recompute() {
    if (!canTrigger || computing) return;
    computing = true; error = '';
    try {
      await triggerNavCompute();
      await load();
    } catch (e) { error = e?.message || 'NAV compute failed'; }
    finally { computing = false; }
  }

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let _teardown = null;
  onMount(() => {
    load();
    _teardown = marketAwareInterval(load, 60000);
  });
  onDestroy(() => { _teardown?.(); });

  function _fmtInr(/** @type {number|null|undefined} */ v) {
    if (v == null || !isFinite(v)) return '—';
    if (Math.abs(v) >= 10000000) return `₹${(v/10000000).toFixed(2)}Cr`;
    if (Math.abs(v) >= 100000)   return `₹${(v/100000).toFixed(2)}L`;
    if (Math.abs(v) >= 1000)     return `₹${(v/1000).toFixed(1)}k`;
    return `₹${Math.round(Number(v))}`;
  }
  function _fmtTs(/** @type {string|null|undefined} */ iso) {
    if (!iso) return '—';
    try { return new Date(iso).toISOString().slice(0, 10); }
    catch { return iso; }
  }
</script>

<svelte:head>
  <title>NAV · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">NAV</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    {#if canTrigger}
      <button type="button" class="btn-primary btn-sm"
              disabled={computing}
              onclick={recompute}>
        {computing ? '…' : 'Recompute now'}
      </button>
    {/if}
    <RefreshButton onClick={load} loading={loading} label="NAV" />
    <PageHeaderActions />
  </span>
</div>

{#if error}
  <div class="nav-error">{error}</div>
{/if}

<!-- Header card — current NAV + day Δ + breakdown. -->
<section class="nav-head">
  {#if latest}
    <div class="nav-headline">
      <div class="nav-headline-main">
        <div class="nav-headline-lbl">Current NAV</div>
        <div class="nav-headline-val">{_fmtInr(latest.nav)}</div>
        <div class="nav-headline-asof">as of {_fmtTs(latest.as_of_date)}</div>
      </div>
      <div class="nav-headline-delta" class:pnl-pos={(dayDelta ?? 0) > 0} class:pnl-neg={(dayDelta ?? 0) < 0}>
        <div class="nav-headline-lbl">Day Δ</div>
        <div class="nav-headline-val">
          {dayDelta == null ? '—' : (dayDelta >= 0 ? '+' : '') + _fmtInr(dayDelta)}
        </div>
        <div class="nav-headline-asof">
          {dayDeltaPct == null ? '—' : (dayDeltaPct >= 0 ? '+' : '') + (dayDeltaPct * 100).toFixed(2) + '%'}
        </div>
      </div>
    </div>
    <div class="nav-stats-grid">
      <div class="stat">
        <div class="stat-lbl">Cash</div>
        <div class="stat-val">{_fmtInr(latest.cash_total)}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Positions MTM</div>
        <div class="stat-val {latest.positions_mtm > 0 ? 'pnl-pos' : latest.positions_mtm < 0 ? 'pnl-neg' : ''}">
          {_fmtInr(latest.positions_mtm)}
        </div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Holdings MTM</div>
        <div class="stat-val">{_fmtInr(latest.holdings_mtm)}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Accounts</div>
        <div class="stat-val nav-acct-list">
          {(latest.accounts_snapshot || []).join(' · ') || '—'}
        </div>
      </div>
    </div>
    {#if latest.note}
      <div class="nav-note">⚠ {latest.note}</div>
    {/if}
  {:else}
    <div class="nav-empty">
      No NAV snapshot yet. The first daily snapshot lands at 16:00 IST
      (or click "Recompute now" if you have permission).
    </div>
  {/if}
</section>

<!-- Per-investor NAV slice (slice 7k). Only shown when the operator
     is signed in AND has a non-zero share_pct — the demo + observer
     case (anonymous or LP without a contribution row) doesn't see
     the card. -->
{#if mySlice && mySlice.share_pct > 0}
  <section class="nav-myslice">
    <div class="nav-myslice-row">
      <div class="nav-myslice-main">
        <div class="nav-myslice-lbl">Your slice
          <span class="nav-myslice-pct">{mySlice.share_pct.toFixed(2)}%</span>
        </div>
        <div class="nav-myslice-val">{_fmtInr(mySlice.nav_share)}</div>
        <div class="nav-myslice-asof">as of {_fmtTs(mySlice.as_of_date)}</div>
      </div>
      <div class="nav-myslice-block" class:pnl-pos={(mySlice.pnl ?? 0) > 0} class:pnl-neg={(mySlice.pnl ?? 0) < 0}>
        <div class="nav-myslice-lbl">P&amp;L</div>
        <div class="nav-myslice-val">
          {mySlice.pnl >= 0 ? '+' : ''}{_fmtInr(mySlice.pnl)}
        </div>
        <div class="nav-myslice-asof">
          {mySlice.pnl_pct == null ? '—' : (mySlice.pnl_pct >= 0 ? '+' : '') + (mySlice.pnl_pct * 100).toFixed(2) + '%'}
        </div>
      </div>
      <div class="nav-myslice-block" class:pnl-pos={(mySlice.day_delta_share ?? 0) > 0} class:pnl-neg={(mySlice.day_delta_share ?? 0) < 0}>
        <div class="nav-myslice-lbl">Day Δ</div>
        <div class="nav-myslice-val">
          {mySlice.day_delta_share == null ? '—' : (mySlice.day_delta_share >= 0 ? '+' : '') + _fmtInr(mySlice.day_delta_share)}
        </div>
        <div class="nav-myslice-asof">
          {mySlice.day_delta_share_pct == null ? '—' : (mySlice.day_delta_share_pct >= 0 ? '+' : '') + (mySlice.day_delta_share_pct * 100).toFixed(2) + '%'}
        </div>
      </div>
      <div class="nav-myslice-block">
        <div class="nav-myslice-lbl">Contributed</div>
        <div class="nav-myslice-val">{_fmtInr(mySlice.contribution)}</div>
      </div>
    </div>
  </section>
{/if}

<!-- NAV curve — single solid amber line. -->
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
  <section class="nav-chart">
    <h2 class="nav-section-heading">NAV curve <span class="nav-meta">{history.length} days</span></h2>
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
  </section>
{/if}

<!-- Composition table — bottom; daily breakdown. -->
{#if history.length > 0}
  <section class="nav-table-section">
    <h2 class="nav-section-heading">Daily composition</h2>
    <div class="nav-table-wrap">
      <table class="nav-table">
        <thead>
          <tr>
            <th>Date</th>
            <th class="th-num">NAV</th>
            <th class="th-num">Cash</th>
            <th class="th-num">Positions MTM</th>
            <th class="th-num">Holdings MTM</th>
            <th>Accounts</th>
            <th>Note</th>
          </tr>
        </thead>
        <tbody>
          {#each [...history].reverse() as r (r.as_of_date)}
            <tr>
              <td class="td-mono">{r.as_of_date}</td>
              <td class="td-num">{_fmtInr(r.nav)}</td>
              <td class="td-num">{_fmtInr(r.cash_total)}</td>
              <td class="td-num {r.positions_mtm > 0 ? 'pnl-pos' : r.positions_mtm < 0 ? 'pnl-neg' : ''}">{_fmtInr(r.positions_mtm)}</td>
              <td class="td-num">{_fmtInr(r.holdings_mtm)}</td>
              <td class="td-mono nav-table-acct">{(r.accounts_snapshot || []).join('·') || '—'}</td>
              <td class="nav-table-note" title={r.note || ''}>{r.note ? '⚠' : ''}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
{/if}

<style>
  .nav-error {
    padding: 0.6rem 0.9rem;
    background: rgba(248, 113, 113, 0.10);
    border: 1px solid rgba(248, 113, 113, 0.40);
    border-radius: 4px;
    color: #fca5a5; font-size: 0.7rem;
    margin-bottom: 0.7rem;
  }

  .nav-head {
    background: rgba(15, 23, 42, 0.45);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
    padding: 1rem 1.2rem;
    margin-bottom: 0.9rem;
  }
  .nav-headline {
    display: flex; align-items: baseline; gap: 1.8rem;
    margin-bottom: 0.9rem;
    flex-wrap: wrap;
  }
  .nav-headline-lbl {
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
  }
  .nav-headline-val {
    font-size: 1.55rem;
    font-weight: 800;
    color: #fbbf24;
    font-family: ui-monospace, monospace;
    line-height: 1;
    margin-top: 0.15rem;
  }
  .nav-headline-delta .nav-headline-val { color: #c8d8f0; }
  .nav-headline-delta.pnl-pos .nav-headline-val { color: #4ade80; }
  .nav-headline-delta.pnl-neg .nav-headline-val { color: #f87171; }
  .nav-headline-asof {
    font-size: 0.6rem;
    color: rgba(155, 176, 208, 0.65);
    font-family: ui-monospace, monospace;
    margin-top: 0.2rem;
  }
  .nav-stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(8rem, 1fr));
    gap: 0.5rem;
  }
  .stat {
    padding: 0.4rem 0.6rem;
    background: rgba(34, 47, 75, 0.50);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .stat-lbl {
    font-size: 0.5rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-weight: 700;
  }
  .stat-val {
    margin-top: 0.15rem;
    font-size: 0.85rem;
    font-weight: 800;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
  }
  .nav-acct-list { font-size: 0.65rem; }
  .nav-note {
    margin-top: 0.6rem;
    padding: 0.5rem 0.7rem;
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.30);
    border-radius: 4px;
    color: #fbbf24;
    font-size: 0.7rem;
  }
  .nav-empty {
    padding: 2rem;
    text-align: center;
    color: #7e97b8;
    font-style: italic;
    font-size: 0.78rem;
  }

  /* Per-investor slice (slice 7k) — smaller, denser card than the
     firm-NAV head. Sits between firm head + curve. */
  .nav-myslice {
    background: linear-gradient(180deg, rgba(34, 211, 238, 0.06),
                                       rgba(15, 23, 42, 0.45));
    border: 1px solid rgba(34, 211, 238, 0.32);
    border-radius: 6px;
    padding: 0.7rem 1rem;
    margin-bottom: 0.9rem;
  }
  .nav-myslice-row {
    display: flex; gap: 1.5rem; flex-wrap: wrap;
    align-items: baseline;
  }
  .nav-myslice-main { flex: 0 0 auto; }
  .nav-myslice-block { flex: 0 0 auto; }
  .nav-myslice-lbl {
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
  }
  .nav-myslice-pct {
    margin-left: 0.4rem;
    font-size: 0.55rem;
    color: #67e8f9;
    font-weight: 800;
  }
  .nav-myslice-val {
    font-size: 1.1rem;
    font-weight: 800;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    line-height: 1;
    margin-top: 0.15rem;
  }
  .nav-myslice-main .nav-myslice-val { color: #67e8f9; }
  .nav-myslice-block.pnl-pos .nav-myslice-val { color: #4ade80; }
  .nav-myslice-block.pnl-neg .nav-myslice-val { color: #f87171; }
  .nav-myslice-asof {
    font-size: 0.55rem;
    color: rgba(155, 176, 208, 0.65);
    font-family: ui-monospace, monospace;
    margin-top: 0.2rem;
  }

  .nav-chart { margin-bottom: 0.9rem; }
  .nav-section-heading {
    margin: 0 0 0.4rem;
    font-size: 0.65rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #a3b9d0;
    font-family: ui-monospace, monospace;
  }
  .nav-meta {
    color: rgba(155, 176, 208, 0.65);
    font-weight: 500;
    margin-left: 0.5rem;
    text-transform: none;
  }
  .nav-svg {
    width: 100%;
    height: 280px;
    background: rgba(15, 23, 42, 0.30);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
  }

  .nav-table-wrap {
    overflow-x: auto;
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
  }
  .nav-table {
    width: 100%; border-collapse: collapse;
    font-size: 0.7rem;
  }
  .nav-table th {
    text-align: left;
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
    color: #a3b9d0;
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    background: rgba(15, 23, 42, 0.65);
    font-family: ui-monospace, monospace;
  }
  .nav-table th.th-num { text-align: right; }
  .nav-table td {
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.10);
    color: #c8d8f0;
  }
  .nav-table td.td-num {
    text-align: right;
    font-family: ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
  }
  .nav-table td.td-mono { font-family: ui-monospace, monospace; }
  .nav-table-acct { font-size: 0.6rem; color: #94a3b8; }
  .nav-table-note { text-align: center; cursor: help; color: #fbbf24; }
  .nav-table tbody tr:hover td { background: rgba(34, 211, 238, 0.05); }

  .pnl-pos { color: #4ade80; }
  .pnl-neg { color: #f87171; }

  .btn-sm { font-size: 0.65rem; padding: 0.25rem 0.55rem; }
</style>
