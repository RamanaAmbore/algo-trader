<script>
  /**
   * DashboardTopStrip — compact per-account summary pinned just below
   * the navbar on /dashboard. Visible regardless of which tab is
   * active.
   *
   * Columns (all 1-2 char labels except "Pos Day Delta"):
   *
   *   A              Account code
   *   C              Cash
   *   H              Holdings P&L (lifetime)
   *   H∆             Day delta · holdings (today's move on the holdings book)
   *   P              Positions P&L (lifetime)
   *   Pos Day Delta  Day delta · positions (today's move on the F&O book)
   *
   * Polls /api/holdings + /api/positions + /api/funds every 30s. On
   * broker outage, cells render '—' and a small chip surfaces the
   * upstream error without taking the page down.
   */
  import { onDestroy, onMount } from 'svelte';
  import { fetchHoldings, fetchPositions, fetchFunds } from '$lib/api';
  import { aggCompact } from '$lib/format';

  /** @type {{ rows?: any[], summary?: any[] } | null} */
  let holdings  = $state(null);
  /** @type {{ rows?: any[], summary?: any[] } | null} */
  let positions = $state(null);
  /** @type {{ rows?: any[] } | null} */
  let funds     = $state(null);
  let loading   = $state(false);
  let error     = $state('');
  /** @type {ReturnType<typeof setInterval> | null} */
  let poll = null;

  async function load() {
    loading = true;
    error   = '';
    try {
      const [h, p, f] = await Promise.all([
        fetchHoldings().catch(e => ({ _error: e.message })),
        fetchPositions().catch(e => ({ _error: e.message })),
        fetchFunds().catch(e => ({ _error: e.message })),
      ]);
      const broken = h?._error || p?._error || f?._error;
      if (broken) {
        error    = (broken + '').slice(0, 80);
        holdings = positions = funds = null;
      } else {
        holdings  = h;
        positions = p;
        funds     = f;
      }
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load();
    poll = setInterval(load, 30_000);
  });
  onDestroy(() => { if (poll != null) clearInterval(poll); });

  // ── Per-account aggregation ──────────────────────────────────────
  const sum = (rows, field) =>
    (rows ?? []).reduce((s, r) => s + (Number(r?.[field]) || 0), 0);
  const sumWhere = (rows, predicate, field) =>
    (rows ?? []).filter(predicate).reduce((s, r) => s + (Number(r?.[field]) || 0), 0);

  const accountRows = $derived.by(() => {
    const hRows = holdings?.rows  ?? [];
    const pRows = positions?.rows ?? [];
    const fRows = funds?.rows     ?? [];

    // Account universe: any account that appears in any of the three
    // payloads, sorted by code so the row order is deterministic.
    const accts = [...new Set([
      ...hRows.map(r => r.account),
      ...pRows.map(r => r.account),
      ...fRows.map(r => r.account),
    ])].filter(a => a && a !== 'TOTAL').sort();

    return accts.map(acct => ({
      account:        acct,
      cash:           sumWhere(fRows, r => r.account === acct, 'cash'),
      hld_pnl:        sumWhere(hRows, r => r.account === acct, 'pnl'),
      hld_day_delta:  sumWhere(hRows, r => r.account === acct, 'day_change_val'),
      pos_pnl:        sumWhere(pRows, r => r.account === acct, 'pnl'),
      pos_day_delta:  sumWhere(pRows, r => r.account === acct, 'day_change_val'),
    }));
  });

  const totalRow = $derived.by(() => {
    if (!accountRows.length) return null;
    return {
      account:        'TOTAL',
      cash:           accountRows.reduce((s, r) => s + r.cash, 0),
      hld_pnl:        accountRows.reduce((s, r) => s + r.hld_pnl, 0),
      hld_day_delta:  accountRows.reduce((s, r) => s + r.hld_day_delta, 0),
      pos_pnl:        accountRows.reduce((s, r) => s + r.pos_pnl, 0),
      pos_day_delta:  accountRows.reduce((s, r) => s + r.pos_day_delta, 0),
    };
  });

  /** @param {number|null|undefined} v */
  function fmt(v) {
    if (v == null || !isFinite(v)) return '—';
    return aggCompact(v);
  }
  /** @param {number|null|undefined} v */
  function pnlClass(v) {
    if (v == null || !isFinite(v)) return '';
    return v >= 0 ? 'pos' : 'neg';
  }
</script>

<div class="strip-card">
  <table class="strip-tbl">
    <thead>
      <tr>
        <th class="th-acct" title="Account">A</th>
        <th title="Cash">C</th>
        <th title="Holdings P&L (lifetime)">H</th>
        <th title="Day delta · Holdings (today's move on the holdings book)">H∆</th>
        <th title="Positions P&L (lifetime)">P</th>
        <th class="th-pdd" title="Day delta · Positions (today's move on the F&O book)">Pos Day Delta</th>
      </tr>
    </thead>
    <tbody>
      {#each accountRows as r}
        <tr>
          <td class="td-acct mono">{r.account}</td>
          <td class="num">{fmt(r.cash)}</td>
          <td class="num {pnlClass(r.hld_pnl)}">{fmt(r.hld_pnl)}</td>
          <td class="num {pnlClass(r.hld_day_delta)}">{fmt(r.hld_day_delta)}</td>
          <td class="num {pnlClass(r.pos_pnl)}">{fmt(r.pos_pnl)}</td>
          <td class="num {pnlClass(r.pos_day_delta)}">{fmt(r.pos_day_delta)}</td>
        </tr>
      {/each}
      {#if totalRow}
        <tr class="total-row">
          <td class="td-acct mono">{totalRow.account}</td>
          <td class="num">{fmt(totalRow.cash)}</td>
          <td class="num {pnlClass(totalRow.hld_pnl)}">{fmt(totalRow.hld_pnl)}</td>
          <td class="num {pnlClass(totalRow.hld_day_delta)}">{fmt(totalRow.hld_day_delta)}</td>
          <td class="num {pnlClass(totalRow.pos_pnl)}">{fmt(totalRow.pos_pnl)}</td>
          <td class="num {pnlClass(totalRow.pos_day_delta)}">{fmt(totalRow.pos_day_delta)}</td>
        </tr>
      {:else if !error && !loading}
        <tr><td colspan="6" class="empty">No book data yet.</td></tr>
      {/if}
    </tbody>
  </table>
  {#if error}
    <span class="strip-err" title={error}>broker unavailable</span>
  {/if}
</div>

<style>
  .strip-card {
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-radius: 5px;
    padding: 0.35rem 0.5rem;
    margin-bottom: 0.55rem;
    position: relative;
  }
  .strip-tbl {
    width: 100%;
    border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
  }
  .strip-tbl th {
    text-align: right;
    padding: 0.18rem 0.45rem;
    color: #fbbf24;
    font-weight: 700;
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    white-space: nowrap;
    cursor: help;
    border-bottom: 1px solid rgba(251,191,36,0.25);
  }
  .strip-tbl th.th-acct { text-align: left; }
  .strip-tbl th.th-pdd  { color: #fbbf24; text-transform: none; letter-spacing: 0; font-size: 0.6rem; }
  .strip-tbl td {
    padding: 0.18rem 0.45rem;
    color: #c8d8f0;
    white-space: nowrap;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .strip-tbl tr:last-child td { border-bottom: none; }
  .strip-tbl .td-acct { font-weight: 600; }
  .strip-tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
  .strip-tbl .mono { font-family: ui-monospace, monospace; }
  .strip-tbl .pos { color: #4ade80; }
  .strip-tbl .neg { color: #f87171; }
  .total-row td {
    color: #fbbf24 !important;
    font-weight: 700;
    border-top: 1px solid rgba(251,191,36,0.25);
  }
  .empty {
    text-align: center;
    color: #4e6080;
    padding: 0.4rem 0;
  }

  .strip-err {
    position: absolute;
    top: 0.35rem;
    right: 0.5rem;
    font-size: 0.55rem;
    font-family: ui-monospace, monospace;
    color: #fca5a5;
    background: rgba(239,68,68,0.10);
    border: 1px solid rgba(239,68,68,0.30);
    padding: 0.1rem 0.35rem;
    border-radius: 3px;
    cursor: help;
  }

  @media (max-width: 640px) {
    .strip-card { padding: 0.3rem 0.4rem; }
    .strip-tbl { font-size: 0.62rem; }
    .strip-tbl th { font-size: 0.5rem; padding: 0.16rem 0.32rem; }
    .strip-tbl td { padding: 0.16rem 0.32rem; }
    .strip-tbl th.th-pdd { font-size: 0.55rem; }
  }
</style>
