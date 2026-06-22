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
  import { nowStamp, marketAwareInterval } from '$lib/stores';
  import {
    fetchStrategy, fetchStrategyLots, updateStrategy,
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
  let loading = $state(false);
  let error = $state('');
  let showClosed = $state(true);

  async function load() {
    if (!Number.isFinite(sid)) return;
    loading = true; error = '';
    try {
      const [s, l] = await Promise.all([
        fetchStrategy(sid),
        fetchStrategyLots(sid, { includeClosed: showClosed }),
      ]);
      strat = s;
      lots = Array.isArray(l?.rows) ? l.rows : [];
      totalOpen   = Number(l?.total_open   ?? 0);
      totalClosed = Number(l?.total_closed ?? 0);
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
  <span class="algo-ts">{$nowStamp}</span>
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
      <h2 class="strat-head-name">{strat.name}</h2>
      <span class={strat.is_active ? 'pill-active' : 'pill-inactive'}>
        {strat.is_active ? 'active' : 'paused'}
      </span>
      {#if canEdit}
        <button class="btn-secondary btn-sm" onclick={toggleActive}>
          {strat.is_active ? 'Pause' : 'Activate'}
        </button>
      {/if}
    </div>
    {#if strat.description}
      <p class="strat-head-desc">{strat.description}</p>
    {/if}
    <div class="strat-stats-grid">
      <div class="stat">
        <div class="stat-lbl">Owner</div>
        <div class="stat-val">{strat.owner_username ?? '—'}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Realised P&amp;L</div>
        <div class="stat-val {strat.realised_pnl > 0 ? 'pnl-pos' : strat.realised_pnl < 0 ? 'pnl-neg' : ''}">{_fmtInr(strat.realised_pnl)}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Unrealised P&amp;L</div>
        <div class="stat-val {strat.unrealised_pnl > 0 ? 'pnl-pos' : strat.unrealised_pnl < 0 ? 'pnl-neg' : ''}">{_fmtInr(strat.unrealised_pnl)}</div>
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
        <div class="stat-val">{_fmtInr(strat.capacity_cap_inr)}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Cap util</div>
        <div class="stat-val">{capUtil == null ? '—' : `${capUtil.toFixed(0)}%`}</div>
      </div>
      <div class="stat">
        <div class="stat-lbl">Target σ</div>
        <div class="stat-val">{_fmtPctOpt(strat.target_volatility)}</div>
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
    <div class="strat-table-wrap">
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

  <!-- Snapshot curve placeholder (slice 7c future). The daily 15:45
       IST background task lands the data; this card will plot
       cumulative P&L once strategy_snapshots populates. -->
  <section class="strat-detail-snapshot">
    <h2 class="strat-section-heading">P&amp;L curve <span class="strat-coming-soon">— populates after daily 15:45 IST snapshot</span></h2>
    <div class="strat-curve-placeholder">
      Daily roll-up not yet available for this strategy. Comes online
      after the next scheduled 15:45 IST capture.
    </div>
  </section>
{/if}

<style>
  .strat-error {
    padding: 0.6rem 0.9rem;
    background: rgba(248, 113, 113, 0.10);
    border: 1px solid rgba(248, 113, 113, 0.40);
    border-radius: 4px;
    color: #fca5a5; font-size: 0.7rem; margin-bottom: 0.7rem;
  }

  .back-link {
    margin-right: 0.6rem; color: #7e97b8;
    text-decoration: none; font-size: 0.7rem;
  }
  .back-link:hover { color: #fbbf24; }

  .strat-detail-head {
    background: rgba(15, 23, 42, 0.45);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
    padding: 0.9rem 1.1rem;
    margin-bottom: 0.9rem;
  }
  .strat-head-title-row { display: flex; align-items: center; gap: 0.7rem; }
  .strat-head-name {
    margin: 0; font-size: 0.95rem; font-weight: 800; color: #fbbf24;
  }
  .strat-head-desc {
    margin: 0.4rem 0 0.8rem;
    font-size: 0.72rem; color: #c8d8f0; line-height: 1.45;
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
    font-size: 0.5rem; letter-spacing: 0.06em;
    text-transform: uppercase; color: #7e97b8;
    font-family: ui-monospace, monospace; font-weight: 700;
  }
  .stat-val {
    margin-top: 0.15rem;
    font-size: 0.85rem; font-weight: 800;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
  }

  .strat-section-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.5rem;
  }
  .strat-section-heading {
    margin: 0;
    font-size: 0.65rem; font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: #a3b9d0;
    font-family: ui-monospace, monospace;
  }
  .strat-coming-soon {
    color: rgba(126, 151, 184, 0.7); font-weight: 500;
    text-transform: none; letter-spacing: 0.02em;
  }
  .show-closed {
    font-size: 0.65rem; color: #a3b9d0;
    display: inline-flex; align-items: center; gap: 0.3rem;
  }

  .strat-table-wrap {
    overflow-x: auto;
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
  }
  .strat-table {
    width: 100%; border-collapse: collapse;
    font-size: 0.72rem;
  }
  .strat-table th {
    text-align: left;
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
    color: #a3b9d0;
    font-size: 0.55rem;
    font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase;
    background: rgba(15, 23, 42, 0.65);
    font-family: ui-monospace, monospace;
  }
  .strat-table th.th-num { text-align: right; }
  .strat-table td {
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.10);
    color: #c8d8f0;
  }
  .strat-table td.td-num {
    text-align: right;
    font-family: ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
  }
  .strat-table td.td-mono { font-family: ui-monospace, monospace; }
  .strat-table tbody tr:hover td { background: rgba(34, 211, 238, 0.05); }
  .strat-row-closed td { opacity: 0.65; }
  .strat-empty {
    text-align: center; padding: 1.4rem !important;
    color: #7e97b8; font-style: italic;
  }

  .side-long {
    display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px;
    background: rgba(74,222,128,0.18); color: #4ade80;
    border: 1px solid rgba(74,222,128,0.40);
    font-size: 0.55rem; font-weight: 800; letter-spacing: 0.06em;
    font-family: ui-monospace, monospace;
  }
  .side-short {
    display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px;
    background: rgba(248,113,113,0.18); color: #f87171;
    border: 1px solid rgba(248,113,113,0.40);
    font-size: 0.55rem; font-weight: 800; letter-spacing: 0.06em;
    font-family: ui-monospace, monospace;
  }

  .qty-rem { color: #fbbf24; font-size: 0.65rem; }
  .pnl-pos { color: #4ade80; }
  .pnl-neg { color: #f87171; }

  .pill-active   { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 3px;
                   background: rgba(74,222,128,0.16); color: #4ade80;
                   border: 1px solid rgba(74,222,128,0.40);
                   font-size: 0.6rem; font-weight: 700; letter-spacing: 0.04em;
                   text-transform: uppercase; font-family: ui-monospace, monospace; }
  .pill-inactive { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 3px;
                   background: rgba(126,151,184,0.16); color: #7e97b8;
                   border: 1px solid rgba(126,151,184,0.40);
                   font-size: 0.6rem; font-weight: 700; letter-spacing: 0.04em;
                   text-transform: uppercase; font-family: ui-monospace, monospace; }

  .btn-sm { font-size: 0.6rem; padding: 0.2rem 0.55rem; }

  .strat-detail-snapshot { margin-top: 0.9rem; }
  .strat-curve-placeholder {
    padding: 2rem;
    text-align: center;
    background: rgba(15, 23, 42, 0.30);
    border: 1px dashed rgba(126, 151, 184, 0.30);
    border-radius: 6px;
    color: #7e97b8;
    font-style: italic;
    font-size: 0.75rem;
  }
</style>
