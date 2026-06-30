<script>
  // Glanceable Pos / Day / Hold / Cash strip pinned under the algo navbar.
  // Data flows through marketDataStores (three-tier: memory → localStorage
  // → broker fetch). The stores are module-level singletons so a load()
  // here also populates /dashboard / NavCard without extra network calls.
  // Whole strip is a single link to /dashboard.

  import { onMount, onDestroy, untrack } from 'svelte';
  import { marketAwareInterval, visibleInterval, executionMode } from '$lib/stores';
  import { aggCompact } from '$lib/format';
  import { getInstrument, loadInstruments } from '$lib/data/instruments';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import { cachedRead, cachedWrite, cachedDelete, TTL } from '$lib/data/persistentCache';
  import { getSnapshot, symbolStore, symbolTickCount } from '$lib/data/symbolStore.svelte.js';
  import { isNseOpen, isMcxOpen } from '$lib/marketHours';
  import { positionsStore, holdingsStore, fundsStore } from '$lib/data/marketDataStores.svelte.js';

  // Reactive views into the three-tier stores. The stores pre-populate from
  // localStorage on module init so these are non-empty on first render.
  const positions = $derived(positionsStore.value ?? []);
  const holdings  = $derived(holdingsStore.value  ?? []);
  const funds     = $derived(fundsStore.value      ?? []);
  // Market-state tick — flips between 0/1/2/3 (no markets / NSE / MCX /
  // both) when the session boundary crosses. The _liveDeltaByRow derived
  // reads this to re-run on the boundary even when no other state has
  // changed (otherwise we'd wait up to 30s for the next loadOnce poll
  // before clearing the stale-tick delta after market close).
  let _mktTick = $state(0);
  /** @type {(() => void) | null} */
  let _mktTimer = null;

  // Monotonic counter incremented after each successful 30s poll
  // completion. The tick-flash $effect depends ONLY on this counter,
  // not on the live-LTP-derived sums, so flash animations fire at
  // most once per poll cycle rather than on every SSE tick.
  let _pollCycleStamp = $state(0);
  // Snapshot of _pollCycleStamp at the moment of the closed→open
  // session transition. _livePositionsToday / _liveHoldingsToday read
  // from positions[].day_change_val which is whatever the LAST poll
  // returned — and `marketAwareInterval` pauses overnight, so that
  // last poll is from yesterday's session close, carrying yesterday's
  // MTM. Without this gate, the reset-to-0 inside the freeze effect
  // is immediately overwritten by the stale value until the next 30s
  // poll arrives. We instead hold disp at 0 until the FIRST fresh
  // poll of the new session lands (_pollCycleStamp > the snapshot).
  let _openTransitionStamp = $state(-1);

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let teardown = null;

  // _load — fires the three-tier refresh via marketDataStores. All
  // caching (Tier 1 memory / Tier 2 localStorage / Tier 3 broker fetch)
  // is handled inside each store. Concurrent calls are deduped by the
  // store (second caller awaits the same Promise).
  async function _load() {
    try {
      await Promise.allSettled([
        positionsStore.load(),
        holdingsStore.load(),
        fundsStore.load(),
      ]);
      // Signal that a poll cycle completed. The flash $effect watches
      // this counter, not the live-LTP-derived sums, so flash fires
      // at most once per 30s poll rather than on every SSE tick.
      _pollCycleStamp += 1;
    } catch (_) { /* silent — strip stays at last-good values */ }
  }

  // BH2: live LTP reads come from symbolStore.get(sym) via getSnapshot.
  // SvelteMap's fine-grained reactivity scopes re-runs of
  // _liveDeltaByRow to the specific syms it touches — each tick only
  // re-triggers consumers reading THAT sym, not every consumer. The
  // local _liveLtpSnap mirror is gone; the writable `liveLtp` store is
  // intentionally still kept alive in quoteStream.js for back-compat
  // until BH3 drops it.

  // Local $state mirror of executionMode so the freeze/thaw $effect can
  // track mode transitions without subscribing inside the effect itself.
  // Mid-session SIM↔LIVE switches need to clear the day-delta freeze so
  // the strip doesn't display mixed real-broker + sim-fabricated P&L.
  let _execMode = $state('idle');
  $effect(() => {
    const unsub = executionMode.subscribe(v => { _execMode = v || 'idle'; });
    return unsub;
  });

  // 250 ms-throttled tick clock. _liveDeltaByRow + the freeze effect
  // depend on this instead of per-tick SvelteMap reactivity, so the
  // derived block re-runs at most 4 Hz even when SSE is bursting at
  // 100 ticks/sec. Operator: "order button response is slow. any click
  // event across the page should have the highest priority." Throttling
  // the per-tick scheduler work frees the main thread for click
  // dispatch. The subscribe is registered inside onMount (NOT inside
  // an $effect that reads reactive state) so it stays at one timer.
  let _throttledTick = $state(0);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _tickThrottleTimer = null;
  /** @type {(() => void) | null} */
  let _tickThrottleUnsub = null;
  onMount(() => {
    _tickThrottleUnsub = symbolTickCount.subscribe(() => {
      if (_tickThrottleTimer) return;
      _tickThrottleTimer = setTimeout(() => {
        _throttledTick++;
        _tickThrottleTimer = null;
      }, 250);
    });
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
    _load();
    // Option B hybrid: throttle to 30 s on hidden — positions / holdings
    // / funds are critical data. WS delivers position_filled immediately.
    teardown = marketAwareInterval(_load, 30000, 30_000);
    // Watch the market-session boundary so _liveDeltaByRow can drop
    // the stale-tick delta immediately on close (not 30s late).
    // visibleInterval: pauses when hidden, fires immediately on tab return.
    _mktTick = (isNseOpen() ? 1 : 0) + (isMcxOpen() ? 2 : 0);
    _mktTimer = visibleInterval(() => {
      const next = (isNseOpen() ? 1 : 0) + (isMcxOpen() ? 2 : 0);
      if (next !== _mktTick) _mktTick = next;
    }, 30_000);
    // Restore frozen values from localStorage when the page mounts
    // outside market hours. The freeze/reset $effect writes the
    // current disp$ values to 'strip.frozen' on every live update
    // during open hours, so the most recent write is always the
    // last live state before close. On a fresh-tab mount during
    // overnight / weekend / pre-market window, the operator sees
    // the same closing P&L view they had at the end of the prior
    // session. The Closed → Open transition handler clears the
    // cache (so we don't leak yesterday's frozen values into
    // today's session).
    const open = isNseOpen() || isMcxOpen();
    if (!open) {
      const cached = cachedRead('strip.frozen');
      if (cached?.value && typeof cached.value === 'object') {
        const v = cached.value;
        dispPositionsToday = Number(v.posToday  || 0);
        dispHoldingsToday  = Number(v.hldToday  || 0);
      }
    }
  });
  onDestroy(() => {
    teardown?.();
    flash.dispose();
    _mktTimer?.();   // visibleInterval teardown
    if (_tickThrottleTimer) { clearTimeout(_tickThrottleTimer); _tickThrottleTimer = null; }
    _tickThrottleUnsub?.();
    // _heartbeatTimer is the 450ms pulse decay timer scheduled inside
    // the heartbeat $effect. Latent leak today (strip is layout-
    // persistent so it never unmounts) but the timer would fire into
    // a destroyed component if the strip ever became conditional.
    if (_heartbeatTimer) {
      clearTimeout(_heartbeatTimer);
      _heartbeatTimer = null;
    }
  });

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
  // Memoization: build the delta ONCE per symbolStore + rows change,
  // keyed by tradingsymbol+account. All 5 derived sums read from this
  // Map in O(1) rather than calling _liveDelta() 5× per row per tick.
  // At 20 positions × 90 ticks/sec burst this drops inner-loop calls
  // from ~100 to ~20 (one Map build + five O(1) lookups per tick).
  //
  // BH2: live LTPs sourced via symbolStore.get(sym) (SvelteMap). The
  // $derived.by re-runs only when one of the syms it actually read
  // changes — fine-grained reactivity at the symbol level, not the
  // whole-map churn of the old liveLtp writable. Touch symbolStore.size
  // once at the top so the derived also re-runs when symbols are
  // ADDED to the store (first SSE tick for a previously-unknown sym).
  const _liveDeltaByRow = $derived.by(() => {
    /** @type {Map<string, number>} */
    const m = new Map();
    // Read _throttledTick so the derived re-runs at 4 Hz max — not on
    // every SvelteMap entry change (which fires per-tick). getSnapshot
    // calls below are wrapped in untrack so they don't register
    // per-symbol reactive deps that would defeat the throttle. This
    // is the same scheduler-pressure fix BH6 applied to MarketPulse
    // buildUnified after the order-button saturation incident.
    void _throttledTick;
    // Read _mktTick so the derived re-runs on the market open/close
    // boundary (otherwise we'd wait up to 30s for _load to pick it up).
    void _mktTick;
    // Operator: "are the position calculations are correct?" — outside
    // market hours the SSE store holds the LAST tick from before close
    // while broker.quote() returns today's close, so their per-row
    // divergence sums to phantom P∆. Gate the delta per exchange:
    // MCX rows during MCX hours (09:00–23:30 IST), NSE/BSE/NFO/BFO/CDS
    // rows during NSE hours (09:15–15:30 IST).
    const nseOpen = isNseOpen();
    const mcxOpen = isMcxOpen();
    const _appliesToRow = (/** @type {any} */ row) => {
      const exch = String(row?.exchange || '').toUpperCase();
      if (exch === 'MCX') return mcxOpen;
      return nseOpen;
    };
    // BUGFIX: positions + holdings used to share the same map key
    // `sym + account`. When the same symbol appeared in BOTH for the
    // same account (operator holds long-term INFY + has an intraday
    // INFY position), the holdings loop ran AFTER positions and
    // overwrote the positions delta in the map — so the positions sum
    // picked up the holdings-qty-based delta. Operator: "position
    // profit in nav strip is not correct." Fix: prefix the key by
    // kind ('P' vs 'H') so each kind owns its own delta entry.
    for (const row of positions) {
      if (!_appliesToRow(row)) continue;
      const sym  = String(row?.tradingsymbol || '').toUpperCase();
      const live = untrack(() => getSnapshot(sym)?.ltp);
      if (live == null) continue;
      const pollLtp = Number(row?.last_price || 0);
      const qty     = Number(row?.quantity   || 0);
      if (!pollLtp || !qty) continue;
      const key = 'P\x00' + sym + '\x00' + String(row?.account || '');
      m.set(key, (Number(live) - pollLtp) * qty);
    }
    for (const row of holdings) {
      if (!_appliesToRow(row)) continue;
      const sym  = String(row?.tradingsymbol || '').toUpperCase();
      const live = untrack(() => getSnapshot(sym)?.ltp);
      if (live == null) continue;
      const pollLtp = Number(row?.last_price || 0);
      const qty     = Number(row?.quantity   || 0);
      if (!pollLtp || !qty) continue;
      const key = 'H\x00' + sym + '\x00' + String(row?.account || '');
      m.set(key, (Number(live) - pollLtp) * qty);
    }
    return m;
  });

  /** @param {any} row @param {'P'|'H'} kind */
  function _delta(row, kind) {
    const key = kind + '\x00'
              + String(row?.tradingsymbol || '').toUpperCase()
              + '\x00' + String(row?.account || '');
    return _liveDeltaByRow.get(key) || 0;
  }

  // Operator: "P calculation is not correct. It should show the
  // current position profit. P delta is change in position profit
  // in the day".
  //
  // Two classes of metric on this strip:
  //
  //   LIFETIME (always live, never frozen) — these are "where do I
  //   stand right now" numbers; they never reset on a session
  //   boundary. Render directly from the live derived:
  //     P    = positionsPnl    (lifetime P&L on open + closed-today positions)
  //     Hld  = holdingsTotal   (lifetime unrealised P&L on holdings)
  //     H    = holdingsValue   (current portfolio value)
  //
  //   DAY DELTA (freeze at close, reset at open) — these are "how
  //   far did I move today" numbers. They freeze at the close-of-
  //   session value so the operator sees their end-of-day P&L all
  //   evening + weekend, then zero out at the next market open and
  //   begin tracking the new session's contribution:
  //     P∆   = positionsToday  (today's MTM move on positions)
  //     HD∆  = holdingsToday   (today's MTM move on holdings)
  //
  // Margin / cash / util / liveCash cells are also always-live —
  // they're broker balance-sheet fields without a "day" concept.
  let dispPositionsToday = $state(0);
  let dispHoldingsToday  = $state(0);
  let _prevMktOpen       = $state(false);
  let _prevExecMode      = $state('idle');
  // Throttle the strip.frozen localStorage write to 5s — the freeze
  // effect now runs per SSE tick so P∆ updates live; without the
  // throttle every tick would synchronously hit disk.
  let _stripFrozenLastWrite = 0;

  const _livePositionsPnl = $derived.by(() => {
    let s = 0;
    for (const p of positions) s += Number(p?.pnl || 0) + _delta(p, 'P');
    return s;
  });
  const _livePositionsToday = $derived.by(() => {
    let s = 0;
    for (const p of positions) s += Number(p?.day_change_val || 0) + _delta(p, 'P');
    return s;
  });
  const _liveHoldingsToday = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.day_change_val || 0) + _delta(h, 'H');
    return s;
  });
  const _liveHoldingsTotal = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.pnl || 0) + _delta(h, 'H');
    return s;
  });
  const _liveHoldingsValue = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.cur_val || 0) + _delta(h, 'H');
    return s;
  });

  // Freeze-at-close / reset-at-open for the TWO day-delta metrics.
  // Lifetime P/Hld/H mirror their live derived directly at render
  // time — they're never frozen.
  //
  // Track the live derived values too so the dispPositionsToday /
  // dispHoldingsToday assignment fires on every SSE tick during market
  // hours. Without this the effect's tracked deps were _mktTick +
  // _execMode only — both rare — so P updated per-tick (rendered
  // directly from _livePositionsPnl) while P∆ stayed stuck at the
  // last-poll value until the next 30s poll. Operator: "any change
  // in P is caused by change in P∆ — I see P∆ constant while P is
  // changing. Fix it."
  $effect(() => {
    void _mktTick;
    void _execMode;
    void _livePositionsToday;
    void _liveHoldingsToday;
    const open = isNseOpen() || isMcxOpen();
    const modeChanged = _execMode !== _prevExecMode;
    if ((open && !_prevMktOpen) || modeChanged) {
      // Closed → Open transition OR execution-mode switch. Snapshot the
      // current poll cycle so we suppress stale day_change_val (from the
      // prior session, or from a real-broker poll just before swapping
      // to SIM) until a fresh in-session poll lands. Force _load()
      // immediately instead of waiting up to 30s for the next
      // marketAwareInterval tick. Without the mode-change branch a
      // mid-session SIM↔LIVE flip leaves dispPositionsToday tracking
      // the old engine's P&L until the next poll naturally arrives.
      dispPositionsToday = 0;
      dispHoldingsToday  = 0;
      cachedDelete('strip.frozen');
      _openTransitionStamp = _pollCycleStamp;
      untrack(() => { _load(); });
    }
    _prevMktOpen  = open;
    _prevExecMode = _execMode;
    if (!open) return;
    // Suppress the live-derived assignment until a fresh poll cycle
    // (one completed AFTER the open transition) lands. Until then
    // positions[].day_change_val is stale from yesterday's last poll.
    if (_pollCycleStamp <= _openTransitionStamp) return;
    dispPositionsToday = _livePositionsToday;
    dispHoldingsToday  = _liveHoldingsToday;
    // localStorage write throttled to 5s — the effect now runs per
    // SSE tick (so P∆ tracks P live), but persisting every tick would
    // hammer disk + slow the main thread under bursty load. 5s is
    // tight enough that a page reload mid-session shows the recent
    // value and loose enough that 100 ticks/sec doesn't queue 100
    // synchronous localStorage writes.
    const _now = Date.now();
    if (_now - _stripFrozenLastWrite > 5000) {
      _stripFrozenLastWrite = _now;
      cachedWrite('strip.frozen', {
        posToday: dispPositionsToday,
        hldToday: dispHoldingsToday,
      }, TTL.day * 7);
    }
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

  // Drive the flash helper off poll-cycle completions ONLY (the
  // tick-pulse path was reverted after the operator reported that
  // order buttons stopped firing while it was enabled — the extra
  // store subscriber + setTimeout chain was saturating the scheduler).
  // Liveness during tick bursts is conveyed via the per-cell ticks
  // already (positions/holdings values update via fine-grained
  // SvelteMap reactivity) — flash + summary cells refresh on poll.
  $effect(() => {
    _pollCycleStamp;
    untrack(() => {
      flash.update('P',    _livePositionsPnl);
      flash.update('M',    marginTotal);
      flash.update('U',    utilPct);
      flash.update('Cp',   cashTotal);
      flash.update('Cash', liveCashTotal);
      flash.update('Pd',   dispPositionsToday);
      flash.update('HDd',  dispHoldingsToday);
      flash.update('Hd',   _liveHoldingsTotal);
      flash.update('H',    _liveHoldingsValue);
    });
  });

  // Heartbeat pulse — fires a brief amber tint across the whole strip
  // on every successful poll cycle, independent of whether values
  // moved. Without this, off-hours polls (when values are stable) +
  // the createTickFlash first-sample-baseline rule leave the strip
  // completely static — operator: "I don't see any animation
  // refreshing nav strip". Heartbeat is a faint "data just refreshed"
  // signal, distinct from the per-cell directional flash (which still
  // fires when values move).
  let _heartbeatOn = $state(false);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _heartbeatTimer = null;
  $effect(() => {
    if (_pollCycleStamp === 0) return;  // skip mount paint
    _heartbeatOn = true;
    if (_heartbeatTimer) clearTimeout(_heartbeatTimer);
    _heartbeatTimer = setTimeout(() => {
      _heartbeatOn = false;
      _heartbeatTimer = null;
    }, 450);
  });
</script>

<a class={'ps-strip' + (_heartbeatOn ? ' ps-heartbeat' : '')} href="/dashboard"
   aria-label="Open the dashboard — full positions, holdings, and funds grids">
  <span class="ps-agg" title="Positions: lifetime P&L / today's MTM move (Δ)">
    <span class="ps-agg-k ps-delta">P∆</span>
    <span class={'ps-agg-v ' + (_livePositionsPnl > 0 ? 'ps-pos' : _livePositionsPnl < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('P')}
      >{fmtMoney(_livePositionsPnl)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ' + (dispPositionsToday > 0 ? 'ps-pos' : dispPositionsToday < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Pd')}
      >{fmtMoney(dispPositionsToday)}</span>
  </span>
  <!-- Combined margin chip: available margin (₹) / utilisation (%).
       Saves a slot vs separate M + U chips. Margin value uses cyan
       (cash palette) at positive; utilisation flips through cash/flat/
       neg as the pool fills up (>70% = crowded, 30-70% = mid,
       <30% = lots of room). -->
  <span class="ps-agg" title="Margin: available (₹) / utilisation (% used)">
    <span class="ps-agg-k">M</span>
    <span class={'ps-agg-v ' + (marginTotal > 0 ? 'ps-cash' : marginTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('M')}
      >{fmtMoney(marginTotal)}</span
    >{#if utilPct != null}<span class="ps-agg-sep">/</span
      ><span class={'ps-agg-v ' + (utilPct > 70 ? 'ps-neg' : utilPct > 30 ? 'ps-flat' : 'ps-cash') + ' ' + flash.classOf('U')}
      >{utilPct.toFixed(1)}%</span>{/if}
  </span>
  <!-- Combined cash chip: total cash (incl. premium tied up in long
       options) / cash available (deployable now). Canonical labels
       (Bloomberg PRTU, IBKR Portfolio):
         C  = Total Cash  = CA + Σ(avg × qty for long CE/PE)
         CA = Cash Available — what the operator can deploy now,
              already nets realised P&L, premium debits, blocked
              margin per the broker's books.
       Per-broker drift in how realised M2M is folded into avail.cash
       is documented in the audit memo; if the sum diverges from
       broker apps, the Dhan/Groww adapter math is the first place to
       look. -->
  <span class="ps-agg" title="Cash: total (C, incl. premium tied up in long options) / available (CA, deployable now)">
    <span class="ps-agg-k">C</span>
    <span class={'ps-agg-v ' + (cashTotal > 0 ? 'ps-cash' : cashTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Cp')}
      >{fmtMoney(cashTotal)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ' + (liveCashTotal > 0 ? 'ps-cash' : liveCashTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Cash')}
      >{fmtMoney(liveCashTotal)}</span>
  </span>
  <span class="ps-agg" title="Holdings: lifetime P&L (H∆) / today's MTM move (HD∆)">
    <span class="ps-agg-k ps-delta">H∆</span>
    <span class={'ps-agg-v ' + (_liveHoldingsTotal > 0 ? 'ps-pos' : _liveHoldingsTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Hd')}
      >{fmtMoney(_liveHoldingsTotal)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ' + (dispHoldingsToday > 0 ? 'ps-pos' : dispHoldingsToday < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('HDd')}
      >{fmtMoney(dispHoldingsToday)}</span>
  </span>
  <span class="ps-agg" title="Current holding value — sum of cur_val across holdings">
    <span class="ps-agg-k">H</span>
    <span class={'ps-agg-v ps-cash ' + flash.classOf('H')}>{fmtMoney(_liveHoldingsValue)}</span>
  </span>
</a>

<style>
  .ps-strip {
    /* Fixed band below the navbar (which is also fixed at top:0).
       49px = 48px navbar height + 1px navbar border-bottom, so the
       ps-strip sits flush below the navbar's bottom edge with no
       overlap. Horizontal padding matches .algo-nav-inner /
       .algo-footer (0 0.5rem) so all three sticky-chrome elements
       have inner content aligned along the same vertical lines. */
    position: fixed;
    top: 49px;
    left: 0;
    right: 0;
    z-index: 49;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 0.9rem;
    height: 1.5rem;
    box-sizing: border-box;
    padding: 0 0.5rem;
    /* Slice AT audit fix — was `#0a1020 → #131c33`, an off-token
       second stop. Anchor both stops on the chrome elevation tokens
       so the band reads as a sibling of navbar + footer. */
    background: linear-gradient(180deg, var(--algo-bg-elev1) 0%, #0f1828 100%);
    border-bottom: 1px solid var(--algo-amber-border-soft);
    color: var(--algo-slate);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.6rem;
    letter-spacing: 0.04em;
    text-decoration: none;
    user-select: none;
    transition: background 0.08s, border-bottom-color 0.18s;
  }
  /* Heartbeat — fires on every successful 30s poll completion via a
     class toggle that resolves after 450ms. Subtle amber-bordered
     glow that reads as "data refreshed" without competing with the
     per-cell directional flash. Operator: "I don't see any animation
     refreshing nav strip". */
  .ps-strip.ps-heartbeat {
    border-bottom-color: rgba(251, 191, 36, 0.85);
    background: linear-gradient(180deg, #0a1020 0%, #1a2640 100%);
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
  /* Slash between the two values inside a paired chip. Muted slate
     so the value glyphs read as the primary signal and the slash is
     just a divider. Zero margin; the surrounding HTML is written with
     no whitespace either side of the slash so it sits flush against
     the values. */
  .ps-agg-sep {
    color: var(--algo-muted);
    margin: 0;
    font-weight: 400;
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
       narrowest devices (~320 px). Horizontal padding matches navbar
       inner + footer on mobile (0 0.25rem) so the inner content
       aligns left/right across the three chrome bands. */
    .ps-strip   { gap: 0.28rem; padding: 0 0.25rem;
                  overflow-x: auto; -webkit-overflow-scrolling: touch;
                  scrollbar-width: none; }
    .ps-strip::-webkit-scrollbar { display: none; }
    .ps-agg     { gap: 0.15rem; flex-shrink: 0; }
    .ps-agg-k   { font-size: 0.5rem; }
    .ps-agg-v   { font-size: 0.6rem; }
  }
  @media (max-width: 380px) {
    /* Narrowest phones — drop one more notch. */
    .ps-strip   { gap: 0.22rem; padding: 0 0.2rem; }
    .ps-agg-k   { font-size: 0.46rem; }
    .ps-agg-v   { font-size: 0.55rem; }
  }
</style>
