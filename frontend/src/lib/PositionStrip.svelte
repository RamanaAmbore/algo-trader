<script>
  // Glanceable position / holdings strip — pinned just under the
  // navbar on every algo page. One static row of aggregates always
  // visible; click to expand a drawer with per-position chips
  // sorted by |day P&L| so the movers are at the front.
  //
  // Reads from dataCache (populated by /dashboard's PerformancePage
  // on mount + every refresh) so no extra API load. When the cache
  // is empty (cold start, anonymous demo on a non-cached page) the
  // strip self-loads positions + holdings via the public /api/
  // endpoints — same surface as the dashboard, identical masking.
  //
  // Industry analogue: IBKR TWS workspace ticker / Bloomberg's
  // workspace strip. Static numbers, not a marquee — operators
  // glance, not track. Above-fold (under the navbar) rather than
  // pinned to the footer where the eye tunes it out.

  import { onMount, onDestroy } from 'svelte';
  import { dataCache, marketAwareInterval } from '$lib/stores';
  import { fetchPositions, fetchHoldings, fetchFunds } from '$lib/api';
  import { aggFmt, pctFmt } from '$lib/format';

  // Local mirror of the cache so the strip re-renders when /dashboard
  // refreshes the snapshot. The cache itself isn't a Svelte store
  // — we self-fetch on mount + refresh on a slow timer.
  let positions = $state(/** @type {any[]} */ ([]));
  let holdings  = $state(/** @type {any[]} */ ([]));
  // Funds rows — one per account. Used only to surface a single
  // aggregated CASH pill on the strip; the dashboard owns the
  // detailed funds grid.
  let funds     = $state(/** @type {any[]} */ ([]));
  // Per-source loaded flags so the strip can distinguish "haven't
  // fetched yet" (show "—") from "fetched and got empty" (show ₹0).
  // The earlier `array.length > 0` heuristic conflated the two.
  let positionsLoaded = $state(false);
  let holdingsLoaded  = $state(false);
  let fundsLoaded     = $state(false);
  let lastRefresh = $state('');

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let teardown = null;

  // Demo / anonymous sessions still get real data (with masked
  // accounts) — same path as /performance. The strip only hides
  // when the operator hasn't loaded yet AND the cache is empty.
  async function loadOnce() {
    try {
      // Cache hit — paint instantly, then refresh in the background.
      // Treat a cached value as "loaded" so we don't flash "—" before
      // the next fetch resolves.
      if (dataCache.positions?.rows) {
        positions = dataCache.positions.rows;
        positionsLoaded = true;
      }
      if (dataCache.holdings?.rows)  {
        holdings  = dataCache.holdings.rows;
        holdingsLoaded = true;
      }
      if (dataCache.funds?.rows) {
        funds = dataCache.funds.rows.filter(
          (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
        );
        fundsLoaded = true;
      }
      const [p, h, f] = await Promise.allSettled([
        fetchPositions(), fetchHoldings(), fetchFunds(),
      ]);
      if (p.status === 'fulfilled') {
        positions = p.value?.rows || [];
        positionsLoaded = true;
        dataCache.positions = p.value;
      }
      if (h.status === 'fulfilled') {
        holdings = h.value?.rows || [];
        holdingsLoaded = true;
        dataCache.holdings = h.value;
      }
      if (f.status === 'fulfilled') {
        // /api/funds returns per-account rows AND a synthesized TOTAL
        // row (already pre-summed by the backend). Including the TOTAL
        // row in the per-row sum below double-counts every value.
        // Strip it on the way in so cashTotal / future aggregates read
        // the per-account rows only.
        funds = (f.value?.rows || []).filter(
          (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
        );
        fundsLoaded = true;
        dataCache.funds = f.value;
      }
      lastRefresh = new Date().toLocaleTimeString('en-IN', {
        hour12: false, hour: '2-digit', minute: '2-digit',
      });
    } catch (_) { /* silent — strip just stays at last-good values */ }
  }

  onMount(() => {
    loadOnce();
    // Slow poll — strip is glanceable, not real-time. /performance
    // and /dashboard refresh every 30 s on their own; we mirror that
    // cadence so we don't hammer /api/positions when the operator
    // sits on a different algo page.
    // marketAwareInterval — strip values are positions/holdings P&L,
    // both static outside NSE/MCX hours. No need to poll overnight.
    teardown = marketAwareInterval(loadOnce, 30000);
  });
  onDestroy(() => { teardown?.(); });

  // ── Aggregates ────────────────────────────────────────────────
  // Three single-letter buckets so each number tells one story:
  //   P  → Positions P/L           (intraday positions; pnl IS the
  //                                  day's number — open + closed)
  //   T  → Holdings Today          (holdings.day_change_val — what
  //                                  the spot moved today)
  //   H  → Holdings Total          (holdings.pnl — total unrealised
  //                                  P/L from entry price)
  // P + T = "today's full P&L". H is the long-running carry.
  const positionsPnl = $derived.by(() => {
    let s = 0;
    for (const p of positions) s += Number(p?.pnl || 0);
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
  // Aggregate cash across accounts. Funds rows expose `cash` as the
  // free balance — the same column the Funds grid surfaces. Prefer
  // numeric coercion + |0| fallback so a row missing the field
  // doesn't break the sum.
  const cashTotal = $derived.by(() => {
    let s = 0;
    for (const f of funds) s += Number(f?.cash || 0);
    return s;
  });

  /** Per-row chip data — merged positions + holdings, sorted by
   *  |day P&L| descending so the movers come first. Each chip
   *  carries enough to identify the symbol + read the day's
   *  direction at a glance.
   *
   *  IMPORTANT: only include rows with NON-ZERO quantity. Kite's
   *  /positions returns closed intraday positions with quantity=0
   *  (so callers can still read realised P&L), and stale holdings
   *  fully sold off mid-day surface the same way. Showing them as
   *  chips makes the strip look like it's tracking ghosts. The
   *  aggregate (DAY / TOTAL above) DOES keep their P&L — closed
   *  positions still contributed to the day's move. */
  const chips = $derived.by(() => {
    const out = [];
    for (const p of positions) {
      const qty = Number(p?.quantity || 0);
      if (qty === 0) continue;        // closed intraday — history only
      const dayChg = Number(p?.pnl || 0);
      const ltp    = Number(p?.close_price || 0);
      // Position day-pct ≈ pnl / (avg × |qty|). Avg can be 0 on a
      // freshly-opened intraday — fall back to ltp so we still get
      // a magnitude.
      const denom  = Math.abs(Number(p?.average_price || ltp || 0) * qty);
      const dayPct = denom > 0 ? (dayChg / denom) * 100 : 0;
      out.push({
        kind:    'position',
        symbol:  String(p?.tradingsymbol || ''),
        qty,
        ltp,
        dayChg,
        dayPct,
        account: String(p?.account || ''),
      });
    }
    for (const h of holdings) {
      const qty = Number(h?.quantity || 0);
      if (qty === 0) continue;        // sold-off holding — no live exposure
      out.push({
        kind:    'holding',
        symbol:  String(h?.tradingsymbol || ''),
        qty,
        ltp:     Number(h?.close_price || 0),
        dayChg:  Number(h?.day_change_val || 0),
        dayPct:  Number(h?.day_change_percentage || 0),
        account: String(h?.account || ''),
      });
    }
    out.sort((a, b) => Math.abs(b.dayChg) - Math.abs(a.dayChg));
    return out;
  });

  // Counts shown in the meta pill — only LIVE rows (qty != 0).
  // Mismatch with chips.length is impossible by construction.
  const livePositionCount = $derived(positions.filter(p => Number(p?.quantity || 0) !== 0).length);
  const liveHoldingCount  = $derived(holdings.filter(h => Number(h?.quantity || 0) !== 0).length);

  const hasContent = $derived(chips.length > 0);

  /** Format an aggregate ₹ value. Always renders the numeric value
   *  (₹0 when the underlying feed is empty or hasn't loaded yet) —
   *  the previous "—" placeholder caused persistent dashes when a
   *  fetch failed silently or the dataCache wasn't populated by a
   *  prior /dashboard visit. */
  function fmtMoney(/** @type {number} */ v) {
    if (!isFinite(v)) return '₹0';
    return `₹${aggFmt(v)}`;
  }
  function fmtPct(/** @type {number} */ v) {
    if (!isFinite(v) || v === 0) return '0.00%';
    const sign = v < 0 ? '−' : '';
    return `${sign}${pctFmt(Math.abs(v))}%`;
  }
</script>

<!-- Always render the strip — even when there's no data the navbar
     should keep its full chrome row underneath. Aggregates fall back
     to ₹0 cleanly; mover chips stay conditional. -->
{#if true}
  <!-- Whole strip is one link to /dashboard — single click target,
       no expand-collapse interaction to learn. The top-3 movers
       still surface inline so the strip stays glanceable; the
       dashboard remains the place for the full grid. -->
  <a class="ps-strip" href="/dashboard"
     aria-label="Open the dashboard — full positions, holdings, and funds grids">
    <span class="ps-agg" title="Positions P/L — open + closed intraday">
      <span class="ps-agg-k">Pos</span>
      <span class={'ps-agg-v ' + (positionsPnl > 0 ? 'ps-pos' : positionsPnl < 0 ? 'ps-neg' : 'ps-flat')}>
        {fmtMoney(positionsPnl)}
      </span>
    </span>
    <span class="ps-agg" title="Holdings — today's mark-to-market move (day_change_val)">
      <span class="ps-agg-k">Day</span>
      <span class={'ps-agg-v ' + (holdingsToday > 0 ? 'ps-pos' : holdingsToday < 0 ? 'ps-neg' : 'ps-flat')}>
        {fmtMoney(holdingsToday)}
      </span>
    </span>
    <span class="ps-agg" title="Holdings — total unrealised P/L from entry">
      <span class="ps-agg-k">Hold</span>
      <span class={'ps-agg-v ' + (holdingsTotal > 0 ? 'ps-pos' : holdingsTotal < 0 ? 'ps-neg' : 'ps-flat')}>
        {fmtMoney(holdingsTotal)}
      </span>
    </span>
    <span class="ps-agg" title="Cash — free balance summed across accounts">
      <span class="ps-agg-k">Cash</span>
      <span class={'ps-agg-v ' + (cashTotal > 0 ? 'ps-cash' : cashTotal < 0 ? 'ps-neg' : 'ps-flat')}>
        {fmtMoney(cashTotal)}
      </span>
    </span>
    <span class="ps-agg ps-agg-meta">
      <span class="ps-agg-k">{livePositionCount}P · {liveHoldingCount}H</span>
    </span>
    {#if chips.length > 0}
      <!-- Inline top-3 movers — stays glanceable without an expand
           toggle. Tap-through still goes to /dashboard. -->
      <span class="ps-preview">
        {#each chips.slice(0, 3) as c (c.kind + '|' + c.symbol)}
          <span class={'ps-preview-chip ' + (c.dayChg > 0 ? 'ps-pos-bg' : c.dayChg < 0 ? 'ps-neg-bg' : '')}>
            <span class="ps-preview-sym">{c.symbol}</span>
            <span class="ps-preview-pct">{fmtPct(c.dayPct)}</span>
          </span>
        {/each}
      </span>
    {/if}
    {#if lastRefresh}
      <span class="ps-refresh" title="Last refreshed (auto every 30 s)">{lastRefresh}</span>
    {/if}
  </a>
{/if}

<style>
  /* Strip — single full-width <a> link to /dashboard, pinned just
     under the navbar (parent layout slot puts us between <header>
     and <main>). Dark palette matches the algo nav so the strip
     reads as chrome, not page content. Whole element is one click
     target — no expand, no nested buttons. */
  .ps-strip {
    /* Sticky just below the navbar so the aggregates remain visible
       as the operator scrolls. The navbar itself sits at top: 0
       (z-index 50); the strip sits underneath and stacks via top:
       <navbar-height>. ~50px matches the algo navbar's natural
       rendered height. */
    position: sticky;
    top: 50px;
    z-index: 49;
    display: flex;
    align-items: center;
    /* Left-align the cluster so it lines up with the page-content
       margin below. Centering pushed the numbers off the operator's
       primary scan column. */
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

  /* Aggregates — single-letter labels (P / T / H) in muted grey,
     values color-coded vs zero. */
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
  /* Meta count flows naturally — no margin-left: auto. With the
     row centered, pushing the count to the right would offset
     the group's centre of mass. */
  .ps-agg-meta { color: rgba(180,200,230,0.55); }

  .ps-pos  { color: #4ade80; }
  .ps-neg  { color: #f87171; }
  .ps-flat { color: #c8d8f0; }
  /* Cash pill — sky-cyan to read as a balance/inventory pill rather
     than a P&L pill. Negative cash (margin debt) still flips to
     red via .ps-neg so the operator catches it instantly. */
  .ps-cash { color: #7dd3fc; }

  .ps-pos-bg { background: rgba(74,222,128,0.10); }
  .ps-neg-bg { background: rgba(248,113,113,0.10); }

  .ps-refresh {
    color: rgba(180,200,230,0.5);
    font-size: 0.5rem;
    margin-left: 0.5rem;
  }

  /* Inline preview chips — top 3 movers, glanceable without
     expand. */
  .ps-preview {
    display: inline-flex;
    gap: 0.35rem;
    flex: 1 1 auto;
    overflow: hidden;
  }
  .ps-preview-chip {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    padding: 0.05rem 0.4rem;
    border-radius: 2px;
    border: 1px solid rgba(255,255,255,0.06);
    font-size: 0.55rem;
    white-space: nowrap;
    max-width: 12rem;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ps-preview-sym {
    color: #c8d8f0;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ps-preview-pct {
    color: inherit;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  @media (max-width: 640px) {
    /* Tightened so Pos / Day / Hold / Cash all fit on one line on a
       phone in portrait. Earlier the gap was 0.45rem + each pill had
       0.3rem internal label-value gap, which pushed Cash onto a
       second row when any value crossed 6 digits. */
    .ps-strip   { gap: 0.3rem; padding: 0.25rem 0.45rem; }
    .ps-agg     { gap: 0.2rem; }
    .ps-agg-k   { font-size: 0.55rem; }
    .ps-agg-v   { font-variant-numeric: tabular-nums; }
    .ps-preview { display: none; }  /* mobile reads aggregates only */
    .ps-refresh { display: none; }
    .ps-agg-meta { display: none; }
  }
</style>
