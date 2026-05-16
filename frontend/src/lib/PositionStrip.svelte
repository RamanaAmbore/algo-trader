<script>
  // Glanceable Pos / Day / Hold / Cash strip pinned under the algo navbar.
  // Reads from the shared dataCache for fast paint, then refreshes via the
  // same /api/positions, /api/holdings, /api/funds endpoints /performance
  // and /dashboard use. Whole strip is a single link to /dashboard.

  import { onMount, onDestroy } from 'svelte';
  import { dataCache, marketAwareInterval } from '$lib/stores';
  import { fetchPositions, fetchHoldings, fetchFunds } from '$lib/api';
  import { aggCompact } from '$lib/format';

  let positions = $state(/** @type {any[]} */ ([]));
  let holdings  = $state(/** @type {any[]} */ ([]));
  let funds     = $state(/** @type {any[]} */ ([]));

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let teardown = null;

  async function loadOnce() {
    try {
      if (dataCache.positions?.rows) positions = dataCache.positions.rows;
      if (dataCache.holdings?.rows)  holdings  = dataCache.holdings.rows;
      if (dataCache.funds?.rows) {
        funds = dataCache.funds.rows.filter(
          (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
        );
      }
      const [p, h, f] = await Promise.allSettled([
        fetchPositions(), fetchHoldings(), fetchFunds(),
      ]);
      if (p.status === 'fulfilled') {
        positions = p.value?.rows || [];
        dataCache.positions = p.value;
      }
      if (h.status === 'fulfilled') {
        holdings = h.value?.rows || [];
        dataCache.holdings = h.value;
      }
      if (f.status === 'fulfilled') {
        // /api/funds emits a TOTAL row alongside per-account rows;
        // including it in cashTotal would double-count.
        funds = (f.value?.rows || []).filter(
          (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
        );
        dataCache.funds = f.value;
      }
    } catch (_) { /* silent — strip stays at last-good values */ }
  }

  onMount(() => {
    loadOnce();
    teardown = marketAwareInterval(loadOnce, 30000);
  });
  onDestroy(() => { teardown?.(); });

  // P    = positions P&L lifetime (open + closed intraday).
  // M    = available margin summed across accounts.
  // C    = opening cash balance summed across accounts (start of day).
  // Cl   = live cash — decreases when option premium is debited
  //        (this is what the operator usually checks before placing
  //        another long-options order; if it's tight, the next
  //        premium debit will fail).
  // HD∆  = today's mark-to-market move on holdings (day_change_val).
  // Hld  = total unrealised P&L on holdings since entry.
  // H    = current holding value (cur_val sum across holdings).
  // P∆   = today's mark-to-market move on positions (day_change_val).
  const positionsPnl = $derived.by(() => {
    let s = 0;
    for (const p of positions) s += Number(p?.pnl || 0);
    return s;
  });
  const positionsToday = $derived.by(() => {
    let s = 0;
    for (const p of positions) s += Number(p?.day_change_val || 0);
    return s;
  });
  const holdingsToday = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.day_change_val || 0);
    return s;
  });
  const holdingsTotal = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.pnl || 0);
    return s;
  });
  const holdingsValue = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.cur_val || 0);
    return s;
  });
  const cashTotal = $derived.by(() => {
    let s = 0;
    for (const f of funds) s += Number(f?.cash || 0);
    return s;
  });
  // Live cash — Kite's `avail.cash` (= live_balance) summed across
  // accounts. Falls back to `cash` if the backend hasn't surfaced
  // `live_cash` yet (older deploys).
  const liveCashTotal = $derived.by(() => {
    let s = 0;
    for (const f of funds) {
      const lc = Number(f?.live_cash ?? 0);
      s += lc !== 0 ? lc : Number(f?.cash || 0);
    }
    return s;
  });
  const marginTotal = $derived.by(() => {
    let s = 0;
    for (const f of funds) s += Number(f?.avail_margin || 0);
    return s;
  });

  function fmtMoney(/** @type {number} */ v) {
    if (!isFinite(v)) return '0';
    return aggCompact(v);
  }
</script>

<a class="ps-strip" href="/dashboard"
   aria-label="Open the dashboard — full positions, holdings, and funds grids">
  <span class="ps-agg" title="Positions P/L — open + closed intraday">
    <span class="ps-agg-k">P</span>
    <span class={'ps-agg-v ' + (positionsPnl > 0 ? 'ps-pos' : positionsPnl < 0 ? 'ps-neg' : 'ps-flat')}>
      {fmtMoney(positionsPnl)}
    </span>
  </span>
  <span class="ps-agg" title="Available margin — summed across accounts">
    <span class="ps-agg-k">M</span>
    <span class={'ps-agg-v ' + (marginTotal > 0 ? 'ps-cash' : marginTotal < 0 ? 'ps-neg' : 'ps-flat')}>
      {fmtMoney(marginTotal)}
    </span>
  </span>
  <span class="ps-agg" title="Cash — opening balance summed across accounts (start of day)">
    <span class="ps-agg-k">C</span>
    <span class={'ps-agg-v ' + (cashTotal > 0 ? 'ps-cash' : cashTotal < 0 ? 'ps-neg' : 'ps-flat')}>
      {fmtMoney(cashTotal)}
    </span>
  </span>
  <span class="ps-agg" title="Live cash — current cash balance after option premium debits (sum across accounts)">
    <span class="ps-agg-k">Cl</span>
    <span class={'ps-agg-v ' + (liveCashTotal > 0 ? 'ps-cash' : liveCashTotal < 0 ? 'ps-neg' : 'ps-flat')}>
      {fmtMoney(liveCashTotal)}
    </span>
  </span>
  <span class="ps-agg" title="Positions Day delta — today's mark-to-market move on positions (day_change_val)">
    <span class="ps-agg-k">P∆</span>
    <span class={'ps-agg-v ' + (positionsToday > 0 ? 'ps-pos' : positionsToday < 0 ? 'ps-neg' : 'ps-flat')}>
      {fmtMoney(positionsToday)}
    </span>
  </span>
  <span class="ps-agg" title="Holdings Day delta — today's mark-to-market move on holdings (day_change_val)">
    <span class="ps-agg-k">HD∆</span>
    <span class={'ps-agg-v ' + (holdingsToday > 0 ? 'ps-pos' : holdingsToday < 0 ? 'ps-neg' : 'ps-flat')}>
      {fmtMoney(holdingsToday)}
    </span>
  </span>
  <span class="ps-agg" title="Holdings — total unrealised P/L from entry">
    <span class="ps-agg-k">H∆</span>
    <span class={'ps-agg-v ' + (holdingsTotal > 0 ? 'ps-pos' : holdingsTotal < 0 ? 'ps-neg' : 'ps-flat')}>
      {fmtMoney(holdingsTotal)}
    </span>
  </span>
  <span class="ps-agg" title="Current holding value — sum of cur_val across holdings">
    <span class="ps-agg-k">H</span>
    <span class="ps-agg-v ps-cash">{fmtMoney(holdingsValue)}</span>
  </span>
</a>

<style>
  .ps-strip {
    /* Sticky below the navbar (which sits at top:0 z-index:50). 50px is
       the navbar's natural rendered height — bump if the navbar grows. */
    position: sticky;
    top: 50px;
    z-index: 49;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 0.9rem;
    width: 100%;
    padding: 0.2rem 0.85rem;
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border-bottom: 1px solid rgba(251,191,36,0.18);
    color: #c8d8f0;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.6rem;
    letter-spacing: 0.04em;
    text-decoration: none;
    user-select: none;
    transition: background 0.08s;
  }
  .ps-strip:hover {
    background: linear-gradient(180deg, #0a1020 0%, #1a2746 100%);
  }

  .ps-agg {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
  }
  .ps-agg-k {
    color: #7e97b8;
    font-size: 0.5rem;
    font-weight: 700;
    text-transform: uppercase;
  }
  .ps-agg-v {
    font-size: 0.7rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .ps-pos  { color: #4ade80; }
  .ps-neg  { color: #f87171; }
  .ps-flat { color: #c8d8f0; }
  /* Negative cash (margin debt) flips to red via .ps-neg. */
  .ps-cash { color: #7dd3fc; }

  @media (max-width: 640px) {
    /* Eight pills (P · M · C · Cl · P∆ · HD∆ · H∆ · H) must fit on a
       phone. We tighten everything globally; values get a slightly
       smaller font; the wrapper allows horizontal scroll as a
       last-resort safety net so nothing clips off-screen on the
       narrowest devices (~320 px). */
    .ps-strip   { gap: 0.28rem; padding: 0.25rem 0.4rem;
                  overflow-x: auto; -webkit-overflow-scrolling: touch;
                  scrollbar-width: none; }
    .ps-strip::-webkit-scrollbar { display: none; }
    .ps-agg     { gap: 0.15rem; flex-shrink: 0; }
    .ps-agg-k   { font-size: 0.5rem; }
    .ps-agg-v   { font-size: 0.6rem; }
  }
  @media (max-width: 380px) {
    /* Narrowest phones — drop one more notch. */
    .ps-strip   { gap: 0.22rem; padding: 0.22rem 0.32rem; }
    .ps-agg-k   { font-size: 0.46rem; }
    .ps-agg-v   { font-size: 0.55rem; }
  }
</style>
