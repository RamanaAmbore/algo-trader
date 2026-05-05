<script>
  // Per-agent P&L attribution (/admin/pnl).
  //
  // Rough attribution: sum of (fill_price - initial_price) × qty × side
  // across FILLED algo_orders, grouped by agent slug. Refreshes on filter
  // change; no auto-poll (this is a retrospective analytical surface, not
  // a live monitor).

  import { onMount } from 'svelte';
  import { authStore, clientTimestamp } from '$lib/stores';
  import { fetchAgentPnL } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';

  // ── State ──────────────────────────────────────────────────────────
  /** @type {any[]} */
  let rows       = $state([]);
  let loading    = $state(true);
  let error      = $state('');
  let refreshedAt = $state('');

  // Filters
  let filterPeriod = $state('today');
  let filterMode   = $state('all');

  // Sort state
  let sortCol  = $state('gross_pnl');
  let sortDir  = $state(/** @type {'asc'|'desc'} */ ('desc'));

  const PERIODS = [
    { label: 'Today',  value: 'today'  },
    { label: 'Week',   value: 'week'   },
    { label: 'Month',  value: 'month'  },
    { label: 'All',    value: 'all'    },
  ];
  const MODES = [
    { label: 'All',   value: 'all'  },
    { label: 'Live',  value: 'live' },
    { label: 'Paper', value: 'paper'},
  ];

  // ── Data loading ───────────────────────────────────────────────────
  async function load() {
    loading = true; error = '';
    try {
      const data = await fetchAgentPnL({ period: filterPeriod, mode: filterMode });
      rows = data?.agents ?? data ?? [];
      refreshedAt = clientTimestamp();
    } catch (e) {
      error = e.message;
      rows = [];
    } finally {
      loading = false;
    }
  }

  onMount(load);

  // ── Sort ───────────────────────────────────────────────────────────
  function setSort(col) {
    if (sortCol === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    else { sortCol = col; sortDir = 'desc'; }
  }

  const SORT_KEYS = {
    agent:       (r) => (r.agent_name ?? '').toLowerCase(),
    orders:      (r) => r.order_count ?? 0,
    filled:      (r) => r.filled_count ?? 0,
    gross_pnl:   (r) => r.gross_pnl ?? 0,
    win_pct:     (r) => r.win_rate ?? 0,
    avg_slippage:(r) => r.avg_slippage ?? 0,
  };

  const sorted = $derived.by(() => {
    const fn = SORT_KEYS[sortCol] ?? SORT_KEYS.gross_pnl;
    return [...rows].sort((a, b) => {
      const av = fn(a), bv = fn(b);
      const cmp = typeof av === 'string' ? av.localeCompare(bv) : (av - bv);
      return sortDir === 'desc' ? -cmp : cmp;
    });
  });

  // ── Helpers ────────────────────────────────────────────────────────
  /** Integer with commas, no decimals. */
  function _n(/** @type {number|null|undefined} */ v) {
    if (v == null) return '—';
    return Math.round(v).toLocaleString('en-IN');
  }

  /** Rupee-formatted with commas, no decimals. Positive = green, negative = red. */
  function _pnl(/** @type {number|null|undefined} */ v) {
    if (v == null) return { text: '—', cls: '' };
    const rounded = Math.round(v);
    const txt = (v < 0 ? '-₹' : '₹') + Math.abs(rounded).toLocaleString('en-IN');
    const cls = v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '';
    return { text: txt, cls };
  }

  /** Percent, 2 decimals. */
  function _pct(/** @type {number|null|undefined} */ v) {
    if (v == null) return '—';
    return v.toFixed(2) + '%';
  }

  /** Sort-arrow indicator. */
  function _arrow(col) {
    if (sortCol !== col) return '';
    return sortDir === 'desc' ? ' ↓' : ' ↑';
  }
</script>

<svelte:head><title>P&L Attribution | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">P&L</h1>
  <InfoHint popup text="Rough P&L attribution — sum of <b>(fill_price − initial_price) × qty × side</b> across all FILLED orders. Chase-slippage proxy, not realised P&L from position pairing. <br><br><b>v1 limitation:</b> rows are grouped by execution engine (<code>sim</code> / <code>paper</code> / <code>live</code> / <code>expiry</code>) — true per-agent grouping requires an <code>agent_id</code> column on <code>algo_orders</code> which doesn't exist yet." />
  {#if refreshedAt}
    <span class="algo-ts">{refreshedAt}</span>
  {/if}
</div>

{#if error}
  <div class="err-banner">{error}</div>
{/if}

<!-- ── Filter bar ──────────────────────────────────────────────────── -->
<div class="filter-bar">
  <div class="filter-item">
    <label class="filter-label" for="pnl-period">Period</label>
    <div class="pill-group" id="pnl-period">
      {#each PERIODS as p}
        <button type="button"
                class="mode-pill"
                class:mode-pill-active={filterPeriod === p.value}
                onclick={() => { filterPeriod = p.value; load(); }}>
          {p.label}
        </button>
      {/each}
    </div>
  </div>
  <div class="filter-item">
    <label class="filter-label" for="pnl-mode">Mode</label>
    <div class="pill-group" id="pnl-mode">
      {#each MODES as m}
        <button type="button"
                class="mode-pill"
                class:mode-pill-active={filterMode === m.value}
                onclick={() => { filterMode = m.value; load(); }}>
          {m.label}
        </button>
      {/each}
    </div>
  </div>
</div>

<!-- ── Table ───────────────────────────────────────────────────────── -->
{#if loading}
  <div class="empty-state">Loading…</div>
{:else if !sorted.length}
  <div class="empty-state">
    No filled orders in this window — nothing to attribute yet.
  </div>
{:else}
  <div class="pnl-table-wrap">
    <table class="pnl-table">
      <thead>
        <tr>
          <th class="th-left" onclick={() => setSort('agent')}
              title="Sort by agent name">
            Agent{_arrow('agent')}
          </th>
          <th onclick={() => setSort('orders')} title="Total orders">
            Orders{_arrow('orders')}
          </th>
          <th onclick={() => setSort('filled')} title="Filled orders">
            Filled{_arrow('filled')}
          </th>
          <th onclick={() => setSort('gross_pnl')} title="Gross P&L (₹)">
            Gross P&L (₹){_arrow('gross_pnl')}
          </th>
          <th onclick={() => setSort('win_pct')} title="Win % = orders with positive P&L / filled orders">
            Win %{_arrow('win_pct')}
          </th>
          <th onclick={() => setSort('avg_slippage')} title="Average slippage per fill (₹)">
            Avg Slippage (₹){_arrow('avg_slippage')}
          </th>
        </tr>
      </thead>
      <tbody>
        {#each sorted as r (r.agent_name)}
          {@const pnl = _pnl(r.gross_pnl)}
          <tr>
            <td class="td-agent">
              <span class="agent-slug">{r.agent_name ?? '—'}</span>
            </td>
            <td class="td-num">{_n(r.order_count)}</td>
            <td class="td-num">{_n(r.filled_count)}</td>
            <td class="td-num {pnl.cls}">{pnl.text}</td>
            <td class="td-num">{_pct((r.win_rate ?? 0) * 100)}</td>
            <td class="td-num">{_n(r.avg_slippage)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="table-footer">
    {sorted.length} agent{sorted.length === 1 ? '' : 's'} ·
    {filterPeriod === 'today' ? 'today' : filterPeriod === 'week' ? 'last 7 days' : filterPeriod === 'month' ? 'last 30 days' : 'all time'} ·
    {filterMode === 'all' ? 'all modes' : filterMode + ' only'}
  </div>
{/if}

<style>
  /* ── Page header ────────────────────────────────────────────────── */
  .page-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.65rem;
  }
  .algo-ts {
    margin-left: auto;
    font-size: 0.6rem;
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .err-banner {
    margin-bottom: 0.5rem;
    padding: 0.25rem 0.65rem;
    border-radius: 3px;
    background: rgba(248,113,113,0.12);
    border: 1px solid rgba(248,113,113,0.35);
    color: #f87171;
    font-size: 0.65rem;
  }

  /* ── Filter bar ─────────────────────────────────────────────────── */
  .filter-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    align-items: flex-end;
    margin-bottom: 0.75rem;
    padding: 0.5rem 0.65rem;
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 4px;
  }
  .filter-item {
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 0.45rem;
  }
  .filter-label {
    font-size: 0.55rem;
    font-weight: 700;
    color: #7e97b8;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    white-space: nowrap;
  }
  .pill-group {
    display: flex;
    gap: 0.2rem;
    flex-wrap: wrap;
  }
  .mode-pill {
    font-size: 0.6rem;
    font-weight: 600;
    padding: 0.15rem 0.55rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.15);
    background: transparent;
    color: #7e97b8;
    cursor: pointer;
    letter-spacing: 0.03em;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .mode-pill:hover { background: rgba(255,255,255,0.06); }
  .mode-pill-active {
    background: rgba(251,191,36,0.16);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }

  /* ── Table ──────────────────────────────────────────────────────── */
  .pnl-table-wrap {
    overflow-x: auto;
    border-radius: 4px;
    border: 1.5px solid rgba(255,255,255,0.10);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45);
  }
  .pnl-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.68rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
  }
  .pnl-table thead tr {
    background: #0a1020;
  }
  .pnl-table th {
    padding: 0.3rem 0.6rem;
    font-size: 0.55rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    border-bottom: 1px solid rgba(251,191,36,0.35);
    border-right: 1px solid rgba(251,191,36,0.18);
    text-align: right;
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }
  .pnl-table th:hover { color: #fde68a; }
  .pnl-table th.th-left { text-align: left; }
  .pnl-table th:last-child { border-right: none; }

  .pnl-table td {
    padding: 0.28rem 0.6rem;
    color: #c8d8f0;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    border-right: 1px solid rgba(255,255,255,0.06);
    vertical-align: middle;
  }
  .pnl-table td:last-child { border-right: none; }
  .pnl-table tbody tr:nth-child(odd) { background: rgba(13,22,42,0.45); }
  .pnl-table tbody tr:hover { background: rgba(251,191,36,0.07); }

  /* Agent cell */
  .td-agent {
    text-align: left;
    white-space: nowrap;
  }
  .agent-slug {
    font-weight: 600;
    color: #fbbf24;
    font-size: 0.66rem;
    display: block;
  }
  .agent-name {
    font-size: 0.6rem;
    color: #7e97b8;
    font-weight: 400;
    display: block;
  }

  /* Numeric cells */
  .td-num {
    text-align: right;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .pnl-pos { color: #4ade80; font-weight: 700; }
  .pnl-neg { color: #f87171; font-weight: 700; }

  /* Footer */
  .table-footer {
    margin-top: 0.4rem;
    font-size: 0.6rem;
    color: #4a5a70;
    text-align: right;
    font-family: ui-monospace, monospace;
  }

  /* Empty state */
  .empty-state {
    padding: 2rem;
    text-align: center;
    color: #4a5a70;
    font-size: 0.7rem;
    font-family: ui-monospace, monospace;
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 4px;
  }

  /* Mobile: scroll table, compact filter */
  @media (max-width: 768px) {
    .filter-bar { gap: 0.4rem; }
    .filter-item { flex-wrap: wrap; }
  }
</style>
