<script>
  /**
   * DashboardTopStrip — live intraday aggregates pinned to the top of
   * /dashboard, visible regardless of which tab (Performance / P&L) is
   * active. Pulls /api/holdings + /api/positions every 30s and exposes:
   *
   *   T   Total P&L (live, holdings + positions)
   *   D   Day P&L   (live, holdings + positions sum of day_change_val)
   *   DH  Day Hld   (holdings only)
   *   DP  Day Pos   (positions only)
   *
   * Labels are 1-2 chars so the strip stays one line on mobile; full
   * names live in title= for hover / long-press.
   *
   * Degrades gracefully when the broker is in outage: cells render '—',
   * a small subdued banner notes the upstream issue. Operator-readable
   * but not alarming.
   */
  import { onDestroy, onMount } from 'svelte';
  import { fetchHoldings, fetchPositions } from '$lib/api';
  import { aggCompact } from '$lib/format';

  /** @type {{ rows?: any[], summary?: any[] } | null} */
  let holdings  = $state(null);
  /** @type {{ rows?: any[], summary?: any[] } | null} */
  let positions = $state(null);
  let loading   = $state(false);
  let error     = $state('');
  /** @type {ReturnType<typeof setInterval> | null} */
  let poll = null;

  async function load() {
    loading = true;
    error   = '';
    try {
      const [h, p] = await Promise.all([
        fetchHoldings().catch(e => ({ _error: e.message })),
        fetchPositions().catch(e => ({ _error: e.message })),
      ]);
      // Treat broker outages as 'no data' rather than blowing up the
      // whole page — keep the strip mounted with '—' values.
      const broken = h?._error || p?._error;
      if (broken) {
        error    = (broken + '').slice(0, 80);
        holdings = null;
        positions = null;
      } else {
        holdings  = h;
        positions = p;
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

  // ── Derived aggregates ───────────────────────────────────────────
  const sum = (rows, field) =>
    (rows ?? []).reduce((s, r) => s + (Number(r?.[field]) || 0), 0);

  const hldRows = $derived(holdings?.rows  ?? []);
  const posRows = $derived(positions?.rows ?? []);

  // PnL totals — sum row-level pnl across both books.
  const totalPnl = $derived.by(() => {
    if (!hldRows.length && !posRows.length) return null;
    return sum(hldRows, 'pnl') + sum(posRows, 'pnl');
  });

  // Day P&L: holdings carry day_change × opening_qty; positions carry
  // (last - close) × qty. broker_apis exposes both as day_change_val.
  const dayHld = $derived.by(() => {
    if (!hldRows.length) return null;
    return sum(hldRows, 'day_change_val');
  });
  const dayPos = $derived.by(() => {
    if (!posRows.length) return null;
    return sum(posRows, 'day_change_val');
  });
  const dayTotal = $derived.by(() => {
    if (dayHld == null && dayPos == null) return null;
    return (dayHld ?? 0) + (dayPos ?? 0);
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

<div class="strip" class:strip-empty={!hldRows.length && !posRows.length}>
  <div class="kv" title="Total P&L (live, holdings + positions)">
    <span class="kv-lbl">T</span>
    <span class="kv-val {pnlClass(totalPnl)}">{fmt(totalPnl)}</span>
  </div>
  <div class="kv" title="Day P&L (live, holdings + positions sum of day_change_val)">
    <span class="kv-lbl">D</span>
    <span class="kv-val {pnlClass(dayTotal)}">{fmt(dayTotal)}</span>
  </div>
  <div class="kv" title="Day P&L · Holdings only">
    <span class="kv-lbl">DH</span>
    <span class="kv-val {pnlClass(dayHld)}">{fmt(dayHld)}</span>
  </div>
  <div class="kv kv-last" title="Day P&L · Positions only">
    <span class="kv-lbl">DP</span>
    <span class="kv-val {pnlClass(dayPos)}">{fmt(dayPos)}</span>
  </div>
  {#if error}
    <span class="strip-err" title={error}>broker unavailable</span>
  {:else if loading && totalPnl == null}
    <span class="strip-load">…</span>
  {/if}
</div>

<style>
  .strip {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0;
    padding: 0.35rem 0.55rem;
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-radius: 5px;
    margin-bottom: 0.5rem;
  }
  .strip-empty .kv-val { color: #4e6080; }
  .kv {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.05rem;
    padding: 0 0.45rem;
    border-right: 1px solid rgba(255,255,255,0.08);
    cursor: help;
    min-width: 0;
  }
  .kv-last { border-right: none; }
  .kv-lbl {
    font-size: 0.55rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .kv-val {
    font-size: 0.78rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    color: #c8d8f0;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .kv-val.pos { color: #4ade80; }
  .kv-val.neg { color: #f87171; }

  .strip-err {
    margin-left: auto;
    font-size: 0.55rem;
    font-family: ui-monospace, monospace;
    color: #fca5a5;
    background: rgba(239,68,68,0.10);
    border: 1px solid rgba(239,68,68,0.30);
    padding: 0.1rem 0.35rem;
    border-radius: 3px;
    cursor: help;
  }
  .strip-load {
    margin-left: auto;
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    color: #7e97b8;
  }

  @media (max-width: 640px) {
    .strip { padding: 0.3rem 0.4rem; }
    .kv { padding: 0 0.32rem; }
    .kv-val { font-size: 0.7rem; }
    .strip-err { font-size: 0.5rem; padding: 0.08rem 0.28rem; }
  }
</style>
