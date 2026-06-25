<script>
  // Glanceable Pos / Day / Hold / Cash strip pinned under the algo navbar.
  // Reads from the shared dataCache for fast paint, then refreshes via the
  // same /api/positions, /api/holdings, /api/funds endpoints /performance
  // and /dashboard use. Whole strip is a single link to /dashboard.

  import { onMount, onDestroy } from 'svelte';
  import { dataCache, marketAwareInterval } from '$lib/stores';
  import { fetchPositions, fetchHoldings, fetchFunds } from '$lib/api';
  import { aggCompact } from '$lib/format';
  import { getInstrument, loadInstruments } from '$lib/data/instruments';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import { cachedRead, cachedWrite, TTL } from '$lib/data/persistentCache';
  import { liveLtp } from '$lib/data/quoteStream';

  let positions = $state(/** @type {any[]} */ ([]));
  let holdings  = $state(/** @type {any[]} */ ([]));
  let funds     = $state(/** @type {any[]} */ ([]));

  // Monotonic counter incremented after each successful 30s poll
  // completion. The tick-flash $effect depends ONLY on this counter,
  // not on the live-LTP-derived sums, so flash animations fire at
  // most once per poll cycle rather than on every SSE tick.
  let _pollCycleStamp = $state(0);

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let teardown = null;

  async function loadOnce() {
    try {
      // Tier 1: in-session dataCache (lives only as long as the JS module
      // is loaded — survives navigation, dies on reload).
      if (dataCache.positions?.rows) positions = dataCache.positions.rows;
      if (dataCache.holdings?.rows)  holdings  = dataCache.holdings.rows;
      if (dataCache.funds?.rows) {
        funds = dataCache.funds.rows.filter(
          (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
        );
      }
      // Tier 2: persistent localStorage — survives reload + deploy. The
      // operator's "data is retained during deployment" requirement
      // sits here. Only consulted when in-session dataCache is empty;
      // skipped silently if expired/missing.
      if (!positions.length) {
        const cP = cachedRead('strip.positions');
        if (cP?.value && Array.isArray(cP.value)) positions = cP.value;
      }
      if (!holdings.length) {
        const cH = cachedRead('strip.holdings');
        if (cH?.value && Array.isArray(cH.value)) holdings = cH.value;
      }
      if (!funds.length) {
        const cF = cachedRead('strip.funds');
        if (cF?.value && Array.isArray(cF.value)) funds = cF.value;
      }
      const [p, h, f] = await Promise.allSettled([
        fetchPositions(), fetchHoldings(), fetchFunds(),
      ]);
      if (p.status === 'fulfilled') {
        positions = p.value?.rows || [];
        dataCache.positions = p.value;
        if (positions.length) cachedWrite('strip.positions', positions, TTL.minute);
      }
      if (h.status === 'fulfilled') {
        holdings = h.value?.rows || [];
        dataCache.holdings = h.value;
        if (holdings.length) cachedWrite('strip.holdings', holdings, TTL.minute);
      }
      if (f.status === 'fulfilled') {
        // /api/funds emits a TOTAL row alongside per-account rows;
        // including it in cashTotal would double-count.
        funds = (f.value?.rows || []).filter(
          (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
        );
        dataCache.funds = f.value;
        if (funds.length) cachedWrite('strip.funds', funds, TTL.minute);
      }
      // Signal that a poll cycle completed. The flash $effect watches
      // this counter, not the live-LTP-derived sums, so flash fires
      // at most once per 30s poll rather than on every SSE tick.
      _pollCycleStamp += 1;
    } catch (_) { /* silent — strip stays at last-good values */ }
  }

  // Local $state mirror of the liveLtp store. Bridging through $state
  // keeps the per-row delta recomputation below O(1) per derived re-run
  // (no store.subscribe overhead in the hot path) and lets the
  // tick-flash $effect see derived-value changes immediately when
  // any tick lands.
  let _liveLtpSnap = $state(/** @type {Record<string, number>} */ ({}));
  $effect(() => {
    const unsub = liveLtp.subscribe(v => { _liveLtpSnap = v || {}; });
    return unsub;
  });

  // Tick-flash — directional pulse when any of the strip's nine
  // pills changes value on the 30s poll. Subtle enough to read as
  // ambient liveness; loud enough that the operator sees a refresh
  // happened without staring at a digit. Threshold 0 because the
  // sums round to ₹1 anyway — every meaningful poll-to-poll delta
  // fires; the first-sample-no-flash gate in the helper prevents
  // a mount paint from flashing every cell.
  const flash = createTickFlash({ threshold: 0, durationMs: 350 });

  onMount(() => {
    // Instruments cache feeds the long-options premium derivation
    // (we read lot_size off each option to compute lots × lot_size
    // explicitly). Silent on failure — derivation falls back to raw
    // quantity then.
    loadInstruments().catch(() => { /* fallback path handles this */ });
    loadOnce();
    teardown = marketAwareInterval(loadOnce, 30000);
  });
  onDestroy(() => { teardown?.(); flash.dispose(); });

  // P    = positions P&L lifetime (open + closed intraday).
  // M    = available margin summed across accounts.
  // Cl   = live cash — decreases when option premium is debited
  //        (this is what the operator checks before placing another
  //        long-options order; if it's tight, the next premium
  //        debit will fail).
  // C    = Cl + cash debited to buy long options currently held
  //        (i.e. cash you would have if every long option were
  //        closed at its entry premium). Derived from the open
  //        positions list: sum of `average_price × |quantity|` for
  //        each long CE/PE row.
  // HD∆  = today's mark-to-market move on holdings (day_change_val).
  // Hld  = total unrealised P&L on holdings since entry.
  // H    = current holding value (cur_val sum across holdings).
  // P∆   = today's mark-to-market move on positions (day_change_val).
  // Delta-replacement helper: the broker's `pnl` field at poll time is
  // computed as `(poll_ltp − avg) × qty + realised`. When a live tick
  // arrives we want pnl(live) = `(live_ltp − avg) × qty + realised`.
  // That equals `broker_pnl + (live − poll_ltp) × qty`, which avoids
  // re-deriving `realised` from scratch. Same trick applies to
  // `day_change_val` (LTP coefficient is also × qty) and to holdings'
  // `cur_val` (= LTP × qty).
  //
  // Memoization: build the delta ONCE per _liveLtpSnap + rows change,
  // keyed by tradingsymbol+account. All 5 derived sums read from this
  // Map in O(1) rather than calling _liveDelta() 5× per row per tick.
  // At 20 positions × 90 ticks/sec burst this drops inner-loop calls
  // from ~100 to ~20 (one Map build + five O(1) lookups per tick).
  const _liveDeltaByRow = $derived.by(() => {
    /** @type {Map<string, number>} */
    const m = new Map();
    const snap = _liveLtpSnap;
    for (const row of positions) {
      const sym  = String(row?.tradingsymbol || '').toUpperCase();
      const live = snap[sym];
      if (live == null) continue;
      const pollLtp = Number(row?.last_price || 0);
      const qty     = Number(row?.quantity   || 0);
      if (!pollLtp || !qty) continue;
      const key = sym + '\x00' + String(row?.account || '');
      m.set(key, (Number(live) - pollLtp) * qty);
    }
    for (const row of holdings) {
      const sym  = String(row?.tradingsymbol || '').toUpperCase();
      const live = snap[sym];
      if (live == null) continue;
      const pollLtp = Number(row?.last_price || 0);
      const qty     = Number(row?.quantity   || 0);
      if (!pollLtp || !qty) continue;
      const key = sym + '\x00' + String(row?.account || '');
      m.set(key, (Number(live) - pollLtp) * qty);
    }
    return m;
  });

  /** @param {any} row */
  function _delta(row) {
    const key = String(row?.tradingsymbol || '').toUpperCase() + '\x00' + String(row?.account || '');
    return _liveDeltaByRow.get(key) || 0;
  }

  const positionsPnl = $derived.by(() => {
    let s = 0;
    for (const p of positions) s += Number(p?.pnl || 0) + _delta(p);
    return s;
  });
  const positionsToday = $derived.by(() => {
    let s = 0;
    for (const p of positions) s += Number(p?.day_change_val || 0) + _delta(p);
    return s;
  });
  const holdingsToday = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.day_change_val || 0) + _delta(h);
    return s;
  });
  const holdingsTotal = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.pnl || 0) + _delta(h);
    return s;
  });
  const holdingsValue = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.cur_val || 0) + _delta(h);
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
  // Cash debited on currently-held long options — derived from the
  // positions list rather than Kite's `util.option_premium` (which
  // mixes in adjustments + day's net debit, not just current open
  // longs). For each long CE/PE row:
  //
  //   num_lots = quantity / lot_size
  //   cash    = average_price × lot_size × num_lots
  //
  // We resolve lot_size from the instruments cache and compute
  // num_lots + the per-lot premium explicitly. Mathematically this
  // equals `average_price × quantity` (since quantity = lot_size ×
  // num_lots after broker_apis applies the multiplier), but going
  // through num_lots makes the formula match the operator's mental
  // model ("4 lots of NIFTY 22000 PE at ₹180 = 4 × 50 × 180 = ₹36k")
  // and gracefully handles any future broker adapter that surfaces
  // qty in lots instead of contracts.
  const longOptionsCashPaid = $derived.by(() => {
    let s = 0;
    for (const p of positions) {
      const sym = String(p?.tradingsymbol || '').toUpperCase();
      const isOpt = sym.endsWith('CE') || sym.endsWith('PE');
      const qty   = Math.abs(Number(p?.quantity) || 0);
      const avg   = Number(p?.average_price) || 0;
      if (!isOpt || Number(p?.quantity) <= 0) continue;
      const inst = getInstrument(sym);
      const lotSize = Number(inst?.ls) || 0;
      if (lotSize > 0) {
        const numLots = qty / lotSize;
        s += avg * lotSize * numLots;
      } else {
        // Instruments cache not loaded yet (or no lot_size for this
        // symbol). qty is in contracts after broker_apis' multiplier,
        // so avg × qty still gives the right total cash paid.
        s += avg * qty;
      }
    }
    return s;
  });
  const cashTotal = $derived(liveCashTotal + longOptionsCashPaid);
  const marginTotal = $derived.by(() => {
    let s = 0;
    for (const f of funds) s += Number(f?.avail_margin || 0);
    return s;
  });
  // Margin utilisation — used / (used + avail). Operator glances at
  // this to know how much room is left to deploy before adding risk.
  const utilPct = $derived.by(() => {
    let used = 0, avail = 0;
    for (const f of funds) {
      used  += Number(f?.used_margin  || 0);
      avail += Number(f?.avail_margin || 0);
    }
    const denom = used + avail;
    return denom > 0 ? (used / denom) * 100 : null;
  });

  function fmtMoney(/** @type {number} */ v) {
    if (!isFinite(v)) return '0';
    return aggCompact(v);
  }

  // Drive the flash helper off poll-cycle completions ONLY.
  // _pollCycleStamp increments inside loadOnce() after a successful
  // broker fetch, so flash fires at most once per 30s poll — not on
  // every live SSE tick. The numbers still update on every tick via
  // the live-LTP-derived sums above; only the animation is throttled.
  // flash.update is a no-op when the value hasn't changed since last
  // call, so the "first sample establishes baseline, no flash on mount"
  // gate inside createTickFlash still works correctly.
  $effect(() => {
    // Read _pollCycleStamp to create a dependency on poll completions.
    // eslint-disable-next-line no-unused-expressions
    _pollCycleStamp;
    flash.update('P',    positionsPnl);
    flash.update('M',    marginTotal);
    flash.update('U',    utilPct);
    flash.update('Cp',   cashTotal);
    flash.update('Cash', liveCashTotal);
    flash.update('Pd',   positionsToday);
    flash.update('HDd',  holdingsToday);
    flash.update('Hd',   holdingsTotal);
    flash.update('H',    holdingsValue);
  });
</script>

<a class="ps-strip" href="/dashboard"
   aria-label="Open the dashboard — full positions, holdings, and funds grids">
  <span class="ps-agg" title="Positions P/L — open + closed intraday">
    <span class="ps-agg-k">P</span>
    <span class={'ps-agg-v ' + (positionsPnl > 0 ? 'ps-pos' : positionsPnl < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('P')}>
      {fmtMoney(positionsPnl)}
    </span>
  </span>
  <span class="ps-agg" title="Available margin — summed across accounts">
    <span class="ps-agg-k">M</span>
    <span class={'ps-agg-v ' + (marginTotal > 0 ? 'ps-cash' : marginTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('M')}>
      {fmtMoney(marginTotal)}
    </span>
  </span>
  {#if utilPct != null}
    <span class="ps-agg" title="Margin utilisation — used / (used + avail). >70% reads as crowded; <30% means most of the pool is free to deploy.">
      <span class="ps-agg-k">U</span>
      <span class={'ps-agg-v ' + (utilPct > 70 ? 'ps-neg' : utilPct > 30 ? 'ps-flat' : 'ps-cash') + ' ' + flash.classOf('U')}>
        {utilPct.toFixed(1)}%
      </span>
    </span>
  {/if}
  <!-- Audit fix (L-5) — single-letter `C` and `Cl` were indistinguishable
       without hover (no tooltip access on mobile). Renamed to `C+`
       (cash including option-tied premium, i.e. "augmented" cash) and
       `Cash` (just live cash). The `+` is a visual cue that this is
       the augmented value; the parent's tooltip explains the math. -->
  <span class="ps-agg" title="Cash+ — live cash + premium tied up in long options (= cash you'd have if every long option were closed at its entry premium). The + indicates the augmented value.">
    <span class="ps-agg-k">C+</span>
    <span class={'ps-agg-v ' + (cashTotal > 0 ? 'ps-cash' : cashTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Cp')}>
      {fmtMoney(cashTotal)}
    </span>
  </span>
  <span class="ps-agg" title="Cash — current live cash balance after option premium debits (sum across accounts)">
    <span class="ps-agg-k">Cash</span>
    <span class={'ps-agg-v ' + (liveCashTotal > 0 ? 'ps-cash' : liveCashTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Cash')}>
      {fmtMoney(liveCashTotal)}
    </span>
  </span>
  <span class="ps-agg" title="Positions Day delta — today's mark-to-market move on positions (day_change_val)">
    <span class="ps-agg-k ps-delta">P∆</span>
    <span class={'ps-agg-v ' + (positionsToday > 0 ? 'ps-pos' : positionsToday < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Pd')}>
      {fmtMoney(positionsToday)}
    </span>
  </span>
  <span class="ps-agg" title="Holdings Day delta — today's mark-to-market move on holdings (day_change_val)">
    <span class="ps-agg-k ps-delta">HD∆</span>
    <span class={'ps-agg-v ' + (holdingsToday > 0 ? 'ps-pos' : holdingsToday < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('HDd')}>
      {fmtMoney(holdingsToday)}
    </span>
  </span>
  <span class="ps-agg" title="Holdings — total unrealised P/L from entry">
    <span class="ps-agg-k ps-delta">H∆</span>
    <span class={'ps-agg-v ' + (holdingsTotal > 0 ? 'ps-pos' : holdingsTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Hd')}>
      {fmtMoney(holdingsTotal)}
    </span>
  </span>
  <span class="ps-agg" title="Current holding value — sum of cur_val across holdings">
    <span class="ps-agg-k">H</span>
    <span class={'ps-agg-v ps-cash ' + flash.classOf('H')}>{fmtMoney(holdingsValue)}</span>
  </span>
</a>

<style>
  .ps-strip {
    /* Sticky below the navbar (which sits at top:0 z-index:50). 48px is
       the navbar's natural rendered height (h-12). Explicit height
       (1.5rem = 24px) keeps the layout math deterministic so the
       page-header strip below can align flush at top:4.5rem. */
    position: sticky;
    top: 48px;
    z-index: 49;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 0.9rem;
    width: 100%;
    height: 1.5rem;
    box-sizing: border-box;
    padding: 0 0.85rem;
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border-bottom: 1px solid var(--algo-amber-border-soft);
    color: var(--algo-slate);
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
    color: var(--algo-muted);
    font-size: 0.55rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .ps-agg-v {
    font-size: 0.7rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .ps-pos  { color: #4ade80; }
  .ps-neg  { color: #f87171; }
  .ps-flat { color: var(--algo-slate); }
  /* Negative cash (margin debt) flips to red via .ps-neg. */
  .ps-cash { color: #7dd3fc; }
  /* Day-delta key labels (P∆ / HD∆ / H∆) — slightly muted + italic so
     they read as "change since open" vs the plain "P" (lifetime P&L). */
  .ps-delta {
    color: rgba(200,216,240,0.7);
    font-style: italic;
  }

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
