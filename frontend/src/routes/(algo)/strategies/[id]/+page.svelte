<!--
  /strategies/[id] — per-strategy detail page (slice 7c).

  Three sections:
    1. Header card — slug · name · owner · active toggle · stats
       (realised + unrealised + open lots + capacity utilisation)
    2. Lot ledger table — every open + closed lot, FIFO order
       (newest opens first). Long ('B') vs short ('S') styled, P&L
       coloured.
    3. (Slice 7c stub) — placeholder for the snapshot P&L curve,
       wires up once strategy_snapshots populates from the daily
       background task.

  Demo / observer can READ everything (view_strategies cap). Edit
  affordances hidden for read-only roles.
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/state';
  import { nowStamp, lastRefreshAt, formatDualTz, marketAwareInterval } from '$lib/stores';
  import {
    fetchStrategy, fetchStrategyLots, fetchStrategySnapshots,
    fetchStrategyMetrics, updateStrategy,
  } from '$lib/api';
  import { userRole, userCaps, hasCap } from '$lib/rbac';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';

  // SvelteKit's typed router only declared `slug?:` for this folder
  // (the existing /strategies tile carries slugs in some link forms).
  // We're indexing by id, so coerce via a permissive cast.
  const sid = $derived(Number((/** @type {any} */ (page.params)).id));
  const canEdit = $derived(hasCap('manage_own_strategies', $userCaps, $userRole));

  /** @type {any} */
  let strat = $state(null);
  /** @type {any[]} */
  let lots = $state([]);
  let totalOpen   = $state(0);
  let totalClosed = $state(0);
  let _showLiveTs = $state(false);
  let loading = $state(false);
  let error = $state('');
  let showClosed = $state(true);

  /** @type {Array<{as_of_date: string, realised_pnl: number, unrealised_pnl: number, total_pnl: number, open_lots_count: number, open_notional: number}>} */
  let snapshots = $state([]);
  let snapDays = $state(90);
  /** @type {{n_samples: number, days: number, mean_daily_pnl: number|null, daily_vol: number|null, downside_vol: number|null, sharpe: number|null, sortino: number|null, max_drawdown: number|null, max_drawdown_pct: number|null, win_rate: number|null, cumulative_pnl: number|null} | null} */
  let metrics = $state(null);

  async function load() {
    if (!Number.isFinite(sid)) return;
    loading = true; error = '';
    try {
      const [s, l, snap, met] = await Promise.all([
        fetchStrategy(sid),
        fetchStrategyLots(sid, { includeClosed: showClosed }),
        fetchStrategySnapshots(sid, { days: snapDays }),
        fetchStrategyMetrics(sid, { days: snapDays }),
      ]);
      strat = s;
      lots = Array.isArray(l?.rows) ? l.rows : [];
      totalOpen   = Number(l?.total_open   ?? 0);
      totalClosed = Number(l?.total_closed ?? 0);
      snapshots = Array.isArray(snap?.rows) ? snap.rows : [];
      metrics = met ?? null;
    } catch (e) { error = e?.message || 'Load failed'; }
    finally { loading = false; }
  }

  async function toggleActive() {
    if (!canEdit || !strat) return;
    try {
      await updateStrategy(sid, { is_active: !strat.is_active });
      await load();
    } catch (e) { error = e?.message || 'Update failed'; }
  }

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let _teardown = null;
  onMount(() => {
    load();
    _teardown = marketAwareInterval(load, 30000);
  });
  onDestroy(() => { _teardown?.(); });

  // Kept local: aggCompact uses uppercase K/L/C with no '₹' prefix and
  // 2-decimal L. This surface uses lowercase 'k', 1-decimal 'k', no Cr
  // band, and a '₹' prefix — intentional compact style for strategy detail.
  function _fmtInr(/** @type {number|null} */ v) {
    if (v == null || !isFinite(v)) return '—';
    if (Math.abs(v) >= 100000) return `₹${(v/100000).toFixed(2)}L`;
    if (Math.abs(v) >= 1000)   return `₹${(v/1000).toFixed(1)}k`;
    return `₹${Number(v).toFixed(0)}`;
  }
  function _fmtPx(/** @type {number|null} */ v) {
    if (v == null || !isFinite(v)) return '—';
    return Number(v).toFixed(2);
  }
  function _fmtPctOpt(/** @type {number|null} */ v) {
    if (v == null) return '—';
    return `${(Number(v) * 100).toFixed(1)}%`;
  }
  function _fmtTs(/** @type {string|null|undefined} */ iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
        hour12: false,
      });
    } catch { return iso; }
  }
  function _capUtil() {
    if (!strat || strat.capacity_cap_inr == null) return null;
    const tied = lots
      .filter(l => l.remaining_qty > 0)
      .reduce((s, l) => s + (l.remaining_qty * l.open_price), 0);
    if (strat.capacity_cap_inr <= 0) return null;
    return (tied / strat.capacity_cap_inr) * 100;
  }
  const capUtil = $derived(_capUtil());
</script>

<svelte:head>
  <title>{strat?.slug || 'Strategy'} · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <a href="/strategies" class="back-link">← Strategies</a>
    <h1 class="page-title-chip">{strat?.slug || '…'}</h1>
  </span>
  <span class="algo-ts-group">
    <span class="algo-ts" class:algo-ts-hidden={_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Live clock — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {$nowStamp}
    </span>
    <span class="algo-ts-vsep" aria-hidden="true">|</span>
    <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Last refresh — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {formatDualTz($lastRefreshAt)}
    </span>
  </span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="strategy" />
    <PageHeaderActions />
  </span>
</div>

{#if error}
  <div class="strat-error">{error}</div>
{/if}

{#if strat}
  <!-- Header card. Stats grid + active toggle. -->
  <section class="strat-detail-head">
    <div class="strat-head-title-row">
      <h2 class="strat-head-name">{strat?.name}</h2>
      <span class={strat?.is_active ? 'pill-active' : 'pill-inactive'}>
        {strat?.is_active ? 'active' : 'paused'}
      </span>
      {#if canEdit}
        <button class="btn-secondary btn-sm" onclick={toggleActive}>
          {strat?.is_active ? 'Pause' : 'Activate'}
        </button>
      {/if}
    </div>
    {#if strat?.description}
      <p class="strat-head-desc">{strat?.description}</p>
    {/if}
    <div class="strat-stats-grid">
      <div class="stat">
        <div class="stat-lbl">Owner</div>
        <div class="stat-val">{strat?.owner_username ?? '—'}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Realised P&amp;L</div>
        <div class="stat-val {(strat?.realised_pnl ?? 0) > 0 ? 'pnl-pos' : (strat?.realised_pnl ?? 0) < 0 ? 'pnl-neg' : ''}">{_fmtInr(strat?.realised_pnl)}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Unrealised P&amp;L</div>
        <div class="stat-val {(strat?.unrealised_pnl ?? 0) > 0 ? 'pnl-pos' : (strat?.unrealised_pnl ?? 0) < 0 ? 'pnl-neg' : ''}">{_fmtInr(strat?.unrealised_pnl)}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Open lots</div>
        <div class="stat-val">{totalOpen}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Closed lots</div>
        <div class="stat-val">{totalClosed}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Capacity cap</div>
        <div class="stat-val">{_fmtInr(strat?.capacity_cap_inr)}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Cap util</div>
        <div class="stat-val">{capUtil == null ? '—' : `${capUtil.toFixed(0)}%`}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Target σ</div>
        <div class="stat-val">{_fmtPctOpt(strat?.target_volatility)}</div>
      </div>
    </div>
  </section>

  <!-- Lot ledger -->
  <section class="strat-detail-lots">
    <div class="strat-section-head">
      <h2 class="strat-section-heading">Lot ledger</h2>
      <label class="show-closed">
        <input type="checkbox" bind:checked={showClosed} onchange={load} />
        Show closed
      </label>
    </div>
    <div class="strat-table-wrap algo-grid-chrome">
      <table class="strat-table">
        <thead>
          <tr>
            <th>Opened (IST)</th>
            <th>Side</th>
            <th>Account</th>
            <th>Symbol</th>
            <th class="th-num">Qty</th>
            <th class="th-num">Open ₹</th>
            <th class="th-num">Close ₹</th>
            <th class="th-num">Realised</th>
            <th>Closed (IST)</th>
          </tr>
        </thead>
        <tbody>
          {#if lots.length === 0 && !loading}
            <tr><td colspan="9" class="strat-empty">
              No lots yet. Place an order with this strategy attached to populate the ledger.
            </td></tr>
          {/if}
          {#each lots as l (l.id)}
            <tr class:strat-row-closed={l.remaining_qty === 0}>
              <td>{_fmtTs(l.opened_at)}</td>
              <td>
                <span class={l.side === 'B' ? 'side-long' : 'side-short'}>
                  {l.side === 'B' ? 'LONG' : 'SHORT'}
                </span>
              </td>
              <td class="td-mono">{l.account}</td>
              <td class="td-mono">{l.symbol}</td>
              <td class="td-num">
                {l.qty}{#if l.remaining_qty !== l.qty}<span class="qty-rem"> ({l.remaining_qty})</span>{/if}
              </td>
              <td class="td-num">{_fmtPx(l.open_price)}</td>
              <td class="td-num">{_fmtPx(l.close_price)}</td>
              <td class="td-num {l.realized_pnl > 0 ? 'pnl-pos' : l.realized_pnl < 0 ? 'pnl-neg' : ''}">
                {l.realized_pnl === 0 ? '—' : _fmtInr(l.realized_pnl)}
              </td>
              <td>{_fmtTs(l.closed_at)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>

  <!-- Risk-adjusted metrics strip — Sharpe, Sortino, max drawdown,
       win rate. Computed from strategy_snapshots; populates after
       at least 2 snapshot days. Until then shows the "needs N days"
       hint instead of placeholder numbers. -->
  <section class="strat-detail-metrics">
    <div class="strat-section-head">
      <h2 class="strat-section-heading">Risk-adjusted metrics</h2>
      {#if metrics && metrics.n_samples > 0}
        <span class="strat-metrics-meta">
          {metrics.n_samples} daily delta{metrics.n_samples === 1 ? '' : 's'} · last {metrics.days}d
        </span>
      {/if}
    </div>
    {#if !metrics || metrics.n_samples < 1}
      <div class="strat-metrics-empty">
        Needs at least 2 daily snapshots. First one lands at 15:45 IST tonight.
      </div>
    {:else}
      <div class="strat-metrics-grid">
        <div class="metric">
          <div class="metric-lbl" title="Annualised Sharpe ratio. (mean daily P&L / stdev daily P&L) × √252. Bloomberg + Sensibull convention. Risk-free rate assumed 0.">Sharpe</div>
          <div class="metric-val {(metrics.sharpe ?? 0) > 1 ? 'pnl-pos' : (metrics.sharpe ?? 0) < 0 ? 'pnl-neg' : ''}">
            {metrics.sharpe == null ? '—' : Number(metrics.sharpe).toFixed(2)}
          </div>
        </div>
        <div class="metric">
          <div class="metric-lbl" title="Sortino ratio — Sharpe variant using only DOWNSIDE volatility (stdev of negative daily deltas). Penalises losing days only.">Sortino</div>
          <div class="metric-val {(metrics.sortino ?? 0) > 1 ? 'pnl-pos' : (metrics.sortino ?? 0) < 0 ? 'pnl-neg' : ''}">
            {metrics.sortino == null ? '—' : Number(metrics.sortino).toFixed(2)}
          </div>
        </div>
        <div class="metric">
          <div class="metric-lbl" title="Max drawdown — largest peak-to-trough drop on cumulative P&L over the window. Lower (closer to 0) is better.">Max DD</div>
          <div class="metric-val pnl-neg">
            {metrics.max_drawdown == null ? '—' : _fmtInr(-Math.abs(metrics.max_drawdown))}
          </div>
        </div>
        <div class="metric">
          <div class="metric-lbl" title="Max drawdown as % of the running peak at that moment. NULL when peak was 0/negative.">Max DD %</div>
          <div class="metric-val pnl-neg">
            {metrics.max_drawdown_pct == null ? '—' : `${(Number(metrics.max_drawdown_pct) * 100).toFixed(1)}%`}
          </div>
        </div>
        <div class="metric">
          <div class="metric-lbl" title="Fraction of days with positive P&L change.">Win rate</div>
          <div class="metric-val">
            {metrics.win_rate == null ? '—' : `${(Number(metrics.win_rate) * 100).toFixed(0)}%`}
          </div>
        </div>
        <div class="metric">
          <div class="metric-lbl" title="Mean P&L change per day, ₹.">Daily avg</div>
          <div class="metric-val {(metrics.mean_daily_pnl ?? 0) > 0 ? 'pnl-pos' : (metrics.mean_daily_pnl ?? 0) < 0 ? 'pnl-neg' : ''}">
            {_fmtInr(metrics.mean_daily_pnl)}
          </div>
        </div>
        <div class="metric">
          <div class="metric-lbl" title="Standard deviation of daily P&L change, ₹. The 'risk' denominator in Sharpe.">Daily vol</div>
          <div class="metric-val">{_fmtInr(metrics.daily_vol)}</div>
        </div>
        <div class="metric">
          <div class="metric-lbl" title="Cumulative P&L (realised + unrealised) as of the most recent snapshot.">Cumulative</div>
          <div class="metric-val {(metrics.cumulative_pnl ?? 0) > 0 ? 'pnl-pos' : (metrics.cumulative_pnl ?? 0) < 0 ? 'pnl-neg' : ''}">
            {_fmtInr(metrics.cumulative_pnl)}
          </div>
        </div>
      </div>
    {/if}
  </section>

  <!-- P&L curve — hand-rolled SVG. Sourced from strategy_snapshots
       (written nightly at 15:45 IST). Three series stacked:
         - total P&L (realised + unrealised) — amber, solid
         - realised — emerald, dashed
         - unrealised — slate, dotted (when meaningful) -->
  <section class="strat-detail-snapshot">
    <div class="strat-section-head">
      <h2 class="strat-section-heading">P&amp;L curve</h2>
      <span class="strat-curve-meta">
        {#if snapshots.length > 0}
          {snapshots.length} day{snapshots.length === 1 ? '' : 's'}
        {/if}
      </span>
    </div>
    {#if snapshots.length === 0}
      <div class="strat-curve-placeholder">
        No daily snapshots yet for this strategy. The first roll-up
        lands at 15:45 IST tonight.
      </div>
    {:else}
      {@const _pad = { l: 50, r: 12, t: 12, b: 24 }}
      {@const W = 720}
      {@const H = 220}
      {@const innerW = W - _pad.l - _pad.r}
      {@const innerH = H - _pad.t - _pad.b}
      {@const totals = snapshots.map(p => p.total_pnl)}
      {@const realised = snapshots.map(p => p.realised_pnl)}
      {@const allVals = [...totals, ...realised, 0]}
      {@const _min = Math.min(...allVals)}
      {@const _max = Math.max(...allVals)}
      {@const _range = (_max - _min) || 1}
      {@const yOf = (v) => _pad.t + innerH - ((v - _min) / _range) * innerH}
      {@const xOf = (i) => _pad.l + (snapshots.length === 1 ? innerW / 2 : (i * innerW) / (snapshots.length - 1))}
      {@const totalPath = snapshots.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i)} ${yOf(p.total_pnl)}`).join(' ')}
      {@const realisedPath = snapshots.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i)} ${yOf(p.realised_pnl)}`).join(' ')}
      {@const zeroY = yOf(0)}
      <svg class="strat-curve-svg" viewBox="0 0 720 220" preserveAspectRatio="none"
           aria-label="Per-strategy P&L curve">

        <!-- Grid lines (5 horizontal). -->
        {#each [0.0, 0.25, 0.5, 0.75, 1.0] as t}
          {@const y = _pad.t + innerH * t}
          {@const v = _max - _range * t}
          <line x1={_pad.l} y1={y} x2={_pad.l + innerW} y2={y}
                stroke="rgba(126,151,184,0.10)" stroke-width="1" />
          <text x={_pad.l - 8} y={y + 3} text-anchor="end"
                fill="rgba(155,176,208,0.55)" font-size="10"
                style="font-family: var(--font-numeric)">
            {Math.abs(v) >= 100000 ? `₹${(v/100000).toFixed(1)}L`
              : Math.abs(v) >= 1000 ? `₹${(v/1000).toFixed(1)}k`
              : `₹${Math.round(v)}`}
          </text>
        {/each}

        <!-- Zero baseline (slightly stronger). -->
        {#if _min < 0 && _max > 0}
          <line x1={_pad.l} y1={zeroY} x2={_pad.l + innerW} y2={zeroY}
                stroke="rgba(126,151,184,0.45)" stroke-width="1"
                stroke-dasharray="3,3" />
        {/if}

        <!-- Realised series (dashed emerald). -->
        <path d={realisedPath} fill="none"
              stroke="#4ade80" stroke-width="1.5"
              stroke-dasharray="4,3" opacity="0.85" />

        <!-- Total series (solid amber, primary). -->
        <path d={totalPath} fill="none"
              stroke="#fbbf24" stroke-width="2" />

        <!-- Endpoint dot — visual anchor on the most recent day. -->
        <circle cx={xOf(snapshots.length - 1)} cy={yOf(totals[totals.length - 1])}
                r="3" fill="#fbbf24" stroke="#0a1020" stroke-width="1" />

        <!-- X-axis date labels — first + middle + last only (compact). -->
        {#if snapshots.length >= 1}
          <text x={xOf(0)} y={H - 6} text-anchor="start"
                fill="rgba(155,176,208,0.55)" font-size="10"
                style="font-family: var(--font-numeric)">{snapshots[0].as_of_date}</text>
        {/if}
        {#if snapshots.length >= 3}
          {@const mid = Math.floor(snapshots.length / 2)}
          <text x={xOf(mid)} y={H - 6} text-anchor="middle"
                fill="rgba(155,176,208,0.55)" font-size="10"
                style="font-family: var(--font-numeric)">{snapshots[mid].as_of_date}</text>
        {/if}
        {#if snapshots.length >= 2}
          <text x={xOf(snapshots.length - 1)} y={H - 6} text-anchor="end"
                fill="rgba(155,176,208,0.55)" font-size="10"
                style="font-family: var(--font-numeric)">{snapshots[snapshots.length - 1].as_of_date}</text>
        {/if}
      </svg>

      <!-- Legend strip. -->
      <div class="strat-curve-legend">
        <span class="leg leg-total">━ Total P&amp;L</span>
        <span class="leg leg-realised">┄ Realised</span>
      </div>
    {/if}
  </section>
{/if}

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  .strat-error {
    padding: 0.6rem 0.9rem;
    background: var(--c-short-10);
    border: 1px solid rgba(248, 113, 113, 0.40);
    border-radius: 4px;
    color: #fca5a5; font-size: var(--fs-lg); margin-bottom: 0.7rem;
  }

  .back-link {
    margin-right: 0.6rem; color: var(--c-muted);
    text-decoration: none; font-size: var(--fs-lg);
  }
  .back-link:hover { color: var(--c-action); }

  .strat-detail-head {
    background: rgba(15, 23, 42, 0.45);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
    padding: 0.9rem 1.1rem;
    margin-bottom: 0.9rem;
  }
  .strat-head-title-row { display: flex; align-items: center; gap: 0.7rem; }
  .strat-head-name {
    margin: 0; font-size: var(--fs-xl); font-weight: 800; color: var(--c-action);
  }
  .strat-head-desc {
    margin: 0.4rem 0 0.8rem;
    font-size: var(--fs-lg); color: #c8d8f0; line-height: 1.45;
  }
  .strat-stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(7.5rem, 1fr));
    gap: 0.5rem;
  }
  .stat {
    padding: 0.4rem 0.6rem;
    background: rgba(34, 47, 75, 0.50);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .stat-lbl {
    font-size: var(--fs-2xs); letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--c-muted);
    font-family: var(--font-numeric); font-weight: 700;
  }
  .stat-val {
    margin-top: 0.15rem;
    font-size: var(--fs-xl); font-weight: 800;
    color: #c8d8f0;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
  }

  .strat-section-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.5rem;
  }
  /* Canonical .algo-card-title palette + typography — operator: "GREEKS
     is good, make every header uniform". Was: fs-md / 800 / slate-muted
     which drifted from every other card heading on the page. */
  .strat-section-heading {
    margin: 0;
    font-size: var(--fs-md); font-weight: 700; letter-spacing: 0.04em;
    text-transform: uppercase; color: var(--c-action);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  .show-closed {
    font-size: var(--fs-md); color: var(--text-muted);
    display: inline-flex; align-items: center; gap: 0.3rem;
  }

  .strat-table-wrap {
    overflow-x: auto;
    /* Canonical outer chrome — upgraded from 1px muted border to
       1.5px slate + inset shadow + gradient bg to match .algo-grid-chrome. */
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
  }
  .strat-table {
    width: 100%; border-collapse: collapse;
    font-size: var(--fs-lg);
  }
  .strat-table th {
    text-align: left;
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
    color: var(--text-muted);
    font-size: var(--fs-xs);
    font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase;
    background: rgba(15, 23, 42, 0.65);
    font-family: var(--font-numeric);
  }
  .strat-table th.th-num { text-align: right; }
  .strat-table td {
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.10);
    color: #c8d8f0;
  }
  .strat-table td.td-num {
    text-align: right;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
  }
  .strat-table td.td-mono { font-family: var(--font-numeric); }
  .strat-table tbody tr:hover td { background: rgba(34, 211, 238, 0.05); }
  .strat-row-closed td { opacity: 0.65; }
  .strat-empty {
    text-align: center; padding: 1.4rem !important;
    color: var(--c-muted); font-style: italic;
  }

  .side-long {
    display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px;
    background: rgba(74,222,128,0.18); color: var(--c-long);
    border: 1px solid rgba(74,222,128,0.40);
    font-size: var(--fs-xs); font-weight: 800; letter-spacing: 0.06em;
    font-family: var(--font-numeric);
  }
  .side-short {
    display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px;
    background: rgba(248,113,113,0.18); color: var(--c-short);
    border: 1px solid rgba(248,113,113,0.40);
    font-size: var(--fs-xs); font-weight: 800; letter-spacing: 0.06em;
    font-family: var(--font-numeric);
  }

  .qty-rem { color: var(--c-action); font-size: var(--fs-md); }
  .pnl-pos { color: var(--c-long); }
  .pnl-neg { color: var(--c-short); }

  .pill-active   { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 3px;
                   background: rgba(74,222,128,0.16); color: var(--c-long);
                   border: 1px solid rgba(74,222,128,0.40);
                   font-size: var(--fs-sm); font-weight: 700; letter-spacing: 0.04em;
                   text-transform: uppercase; font-family: var(--font-numeric); }
  .pill-inactive { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 3px;
                   background: rgba(126,151,184,0.16); color: var(--c-muted);
                   border: 1px solid rgba(126,151,184,0.40);
                   font-size: var(--fs-sm); font-weight: 700; letter-spacing: 0.04em;
                   text-transform: uppercase; font-family: var(--font-numeric); }

  .btn-sm { font-size: var(--fs-sm); padding: 0.2rem 0.55rem; }

  .strat-detail-metrics { margin-top: 0.9rem; }
  .strat-metrics-meta {
    font-size: var(--fs-sm);
    color: rgba(155,176,208,0.65);
    font-family: var(--font-numeric);
  }
  .strat-metrics-empty {
    padding: 1.1rem;
    text-align: center;
    background: rgba(15, 23, 42, 0.30);
    border: 1px dashed rgba(126, 151, 184, 0.30);
    border-radius: 6px;
    color: var(--c-muted);
    font-style: italic;
    font-size: var(--fs-lg);
  }
  .strat-metrics-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(7rem, 1fr));
    gap: 0.5rem;
    margin-top: 0.4rem;
  }
  .metric {
    padding: 0.45rem 0.6rem;
    background: rgba(34, 47, 75, 0.50);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .metric-lbl {
    font-size: var(--fs-2xs);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--c-muted);
    font-family: var(--font-numeric);
    font-weight: 700;
    cursor: help;
  }
  .metric-val {
    margin-top: 0.2rem;
    font-size: var(--fs-xl);
    font-weight: 800;
    color: #c8d8f0;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
  }

  .strat-detail-snapshot { margin-top: 0.9rem; }
  .strat-curve-placeholder {
    padding: 2rem;
    text-align: center;
    background: rgba(15, 23, 42, 0.30);
    border: 1px dashed rgba(126, 151, 184, 0.30);
    border-radius: 6px;
    color: var(--c-muted);
    font-style: italic;
    font-size: var(--fs-lg);
  }
  .strat-curve-meta {
    font-size: var(--fs-sm);
    color: rgba(155,176,208,0.65);
    font-family: var(--font-numeric);
  }
  .strat-curve-svg {
    width: 100%;
    height: 240px;
    background: rgba(15, 23, 42, 0.30);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
    margin-top: 0.4rem;
  }
  .strat-curve-legend {
    display: flex; gap: 1.2rem; justify-content: flex-end;
    margin-top: 0.4rem;
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
  }
  .leg-total    { color: var(--c-action); }
  .leg-realised { color: var(--c-long); }
</style>
