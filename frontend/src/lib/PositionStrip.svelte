<script>
  // Glanceable Pos / Day / Hold / Cash strip pinned under the algo navbar.
  // Data flows through marketDataStores (three-tier: memory → localStorage
  // → broker fetch). The stores are module-level singletons so a load()
  // here also populates /dashboard / NavCard without extra network calls.
  // Whole strip is a single link to /dashboard.

  import { onMount, onDestroy, untrack } from 'svelte';
  import { marketAwareInterval, visibleInterval, executionMode } from '$lib/stores';
  import { aggCompact } from '$lib/format';
  import { getInstrument, loadInstruments, findNearestFuture } from '$lib/data/instruments';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import { cachedRead, cachedWrite, cachedDelete, TTL } from '$lib/data/persistentCache';
  import { getSnapshot, symbolStore, symbolTickCount, tickBus } from '$lib/data/symbolStore.svelte.js';
  import { isMarketOpen, isNseOpen, isMcxOpen } from '$lib/marketHours';
  import { positionsStore, holdingsStore, fundsStore, publishPulseQuotes, bookPollerTick } from '$lib/data/marketDataStores.svelte.js';
  import { resolveUnderlying } from '$lib/data/resolveUnderlying';
  import { expiryPnl } from '$lib/data/expiryPnl';
  import { decomposeSymbol } from '$lib/data/decomposeSymbol';
  import { batchQuote } from '$lib/api';
  import { baseDayPnlForPosition, livePositionDayPnl } from '$lib/data/nav';
  import DayPnlBreakup from './DayPnlBreakup.svelte';

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
  let _dayPnlBreakupOpen = $state(false);
  let _mktTick = $state(0);
  /** @type {(() => void) | null} */
  let _mktTimer = null;

  // Monotonic counter incremented after each successful 30s poll
  // completion. The tick-flash $effect depends ONLY on this counter,
  // not on the live-LTP-derived sums, so flash animations fire at
  // most once per poll cycle rather than on every SSE tick.
  let _pollCycleStamp = $state(0);
  // Consecutive poll-error counter for stale-data visual indicator.
  // Tracked inside _load() after the await, not via $effect, because
  // dataStore sets _error=null at fetch-start — a $effect would reset
  // the counter to 0 on every poll start, making the threshold unreachable.
  let _staleFailCount = $state(0);
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
      _staleFailCount = (positionsStore.error || holdingsStore.error)
        ? _staleFailCount + 1 : 0;
      // After positions are fresh, refresh underlying spot quotes so
      // _expiryProfit can compute intrinsic values with current spots.
      // Runs fire-and-forget (a batchQuote failure should not delay
      // the strip paint). _pollCycleStamp is now driven by bookPollerTick
      // so the flash animation aligns with the book-poller cadence (5 s)
      // rather than this 30 s interval.
      _loadUnderlyingSpots().catch(() => { /* silent */ });
    } catch (_) { /* silent — strip stays at last-good values */ }
  }

  // Fire the poll-cycle stamp at book-poller cadence (default 5 s) so the
  // flash animation and freeze/thaw gate both run at the same frequency as
  // the ticker rather than waiting for this strip's own 30 s interval.
  $effect(() => {
    void bookPollerTick.value;
    untrack(() => { _pollCycleStamp += 1; });
  });

  // Fetch underlying spot quotes for every F&O option position.
  // Mirrors derivatives/+page.svelte `loadUnderlyingQuotes()`:
  //   resolveUnderlying(inst.u, findNearestFuture) → quoteKey → batchQuote
  //   → publishPulseQuotes → symbolStore
  // After this call, getSnapshot("NIFTY 50")?.ltp etc. will return the
  // current spot so _expiryProfit can compute intrinsic correctly.
  // If the derivatives page is also mounted it publishes the same symbols
  // independently — mergeSymbolBatch(ltp_ts=0) handles the collision
  // without conflict; the most recent snapshot_ts wins.
  async function _loadUnderlyingSpots() {
    // Collect unique quote keys for all F&O option positions only
    // (futures use their own tradingsymbol as the spot key — already
    // in symbolStore from _publishPositionsRows — so they don't need
    // a separate batchQuote here).
    /** @type {Set<string>} */
    const keys = new Set();
    const snap = untrack(() => positionsStore.value ?? []);
    for (const p of snap) {
      const sym  = String(p?.tradingsymbol || '').toUpperCase();
      const exch = String(p?.exchange || '').toUpperCase();
      if (!['NFO', 'MCX', 'CDS', 'BFO'].includes(exch)) continue;
      const isOpt = sym.endsWith('CE') || sym.endsWith('PE');
      if (!isOpt) continue;
      // Use decomposeSymbol (pure regex, no cache) as primary so spots
      // are fetched even when the instruments cache is cold. Fall back
      // to getInstrument(sym)?.u only as a secondary resolution path.
      const decomp = decomposeSymbol(sym);
      const root = decomp.root || getInstrument(sym)?.u;
      if (!root) continue;
      const resolved = resolveUnderlying(root, findNearestFuture);
      if (!resolved?.quoteKey) continue;
      keys.add(resolved.quoteKey);
    }
    if (keys.size === 0) return;
    const res = await batchQuote([...keys]);
    publishPulseQuotes(res?.items ?? []);
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
        if (isMarketOpen()) _throttledTick++;
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
  const flash = createTickFlash({ threshold: 0, durationMs: 300 });

  onMount(() => {
    // Instruments cache feeds both the long-options premium derivation
    // (lot_size for the C pill) and the expiry profit (slot 3) which
    // needs findNearestFuture to resolve MCX option underlyings.
    //
    // _load() fires immediately so positions/holdings/funds paint from
    // broker without waiting for instruments. After instruments resolve,
    // _loadUnderlyingSpots() is re-run with the now-warm findNearestFuture
    // so MCX option spots (CRUDEOIL26JUNFUT etc.) land in symbolStore and
    // _expiryProfit picks them up on the next _throttledTick.
    //
    // Without this, the cold-cache first load calls findNearestFuture while
    // the instruments map is empty → MCX options fall back to synthetic
    // "MCX:CRUDEOIL" keys → batchQuote returns no LTP → those legs are
    // skipped by `if (!(spot > 0)) continue` → slot 3 shows only the
    // NIFTY-family contribution (~78k) instead of the full book (~300k+).
    _load();
    loadInstruments()
      .catch(() => { /* fallback — per-leg spot resolution handles misses */ })
      .then(() => _loadUnderlyingSpots().catch(() => {}));
    // Option A (operator-approved, supersedes Option B): fully pause on hidden.
    // Telegram + email cover fills / losses; WS delivers position_filled on return.
    // Immediate refire on tab visible ensures fresh numbers within one tick.
    teardown = marketAwareInterval(_load, 30000);
    // Watch the market-session boundary so _liveDeltaByRow can drop
    // the stale-tick delta immediately on close (not 30s late).
    // visibleInterval: pauses when hidden, fires immediately on tab return.
    _mktTick = (isNseOpen() ? 1 : 0) + (isMcxOpen() ? 2 : 0);
    _mktTimer = visibleInterval(() => {
      const next = (isNseOpen() ? 1 : 0) + (isMcxOpen() ? 2 : 0);
      if (next !== _mktTick) _mktTick = next;
    }, 30_000);
    // Closed-hours display now reads from positions / holdings stores
    // directly (snapshot SSOT). No localStorage restore needed — the
    // freeze $effect below pulls _liveX = Σ snapshot.day_pnl, which is
    // the last in-session value persisted via daily_book. The legacy
    // cache restore went stale during the snapshot-zero-capture outage
    // and never recovered; SSOT path is robust to that class of bug.

    // Tick-bus border shimmer — sky-300 border flash on real SSE LTP ticks.
    // Separate from the amber poll heartbeat; direction ignored (neutral palette).
    // 300ms unified duration. Throttled to 1 Hz (leading-edge, 1000ms window)
    // so a 20-symbol burst doesn't animate the border 20 times. Only the FIRST
    // tick in each 1000ms window triggers the animation; subsequent ticks in
    // the same window are ignored. Re-arm the 300ms decay timer on each leading
    // edge so continuous tick flow keeps the border lit, clearing 300ms after
    // the last window opens. Decay (300ms) < window (1000ms) so the border
    // returns to idle 700ms before the next pulse — clean gap, no overlap.
    // Leading-edge pattern: fires immediately (no setTimeout delay), so the
    // class is visible within one tick of the emit — the 50ms spec assertion
    // continues to pass. No throttle-timer handle needed; only a timestamp.
    _tickBusUnsub = tickBus.subscribe(() => {
      // Do not animate during closed hours — background pollers still emit
      // on tickBus (sparkline, snapshot refresh, performance) and would give
      // a false "live data refreshing" impression.
      if (!isNseOpen() && !isMcxOpen()) return;
      const _now = performance.now();
      if (_now < _tickBorderThrottleUntil) return;
      _tickBorderThrottleUntil = _now + 1000;  // was 250 (4 Hz) → 1000 (1 Hz)
      // Toggle a↔b so the browser sees a new animation-name and restarts
      // the keyframe on every leading-edge tick (same pattern as RefreshButton).
      _tickBorderClass = _tickBorderClass === 'ps-tick-border-a' ? 'ps-tick-border-b' : 'ps-tick-border-a';
      if (_tickBorderTimer) clearTimeout(_tickBorderTimer);
      _tickBorderTimer = setTimeout(() => {
        _tickBorderClass = '';
        _tickBorderTimer = null;
      }, 300);
    });
  });
  onDestroy(() => {
    teardown?.();
    flash.dispose();
    _mktTimer?.();   // visibleInterval teardown
    if (_tickThrottleTimer) { clearTimeout(_tickThrottleTimer); _tickThrottleTimer = null; }
    _tickThrottleUnsub?.();
    // _heartbeatTimer is the 300ms pulse decay timer scheduled inside
    // the heartbeat $effect. Latent leak today (strip is layout-
    // persistent so it never unmounts) but the timer would fire into
    // a destroyed component if the strip ever became conditional.
    if (_heartbeatTimer) {
      clearTimeout(_heartbeatTimer);
      _heartbeatTimer = null;
    }
    _tickBusUnsub?.();
    if (_tickBorderTimer) { clearTimeout(_tickBorderTimer); _tickBorderTimer = null; }
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
  // Helper: populate delta entries for one row array into m.
  // Called from inside $derived.by so positions/holdings reads stay
  // in reactive scope. untrack is preserved per-snapshot so we don't
  // register per-symbol deps that would defeat the 4 Hz throttle.
  // Guard order, Number() coercions, and key format must not change —
  // _delta() consumes this map with the exact same key shape.
  function _addDeltaEntries(
    /** @type {Map<string, number>} */ m,
    /** @type {any[]} */ rows,
    /** @type {'P'|'H'} */ kind,
    /** @type {(row: any) => boolean} */ appliesToRow,
  ) {
    for (const row of rows) {
      if (!appliesToRow(row)) continue;
      const sym  = String(row?.tradingsymbol || '').toUpperCase();
      // Only use SSE ticks (ltp_ts > 0). REST publishers set ltp_ts=0
      // to prevent phantom deltas when batchQuote races the SSE stream.
      const snap = untrack(() => getSnapshot(sym));
      if (!snap || !(snap.ltp_ts > 0)) continue;
      const live = snap.ltp;
      // LTP flicker fix: treat any non-positive live as "no tick yet".
      if (typeof live !== 'number' || !(live > 0)) continue;
      const pollLtp = Number(row?.last_price || 0);
      const qty     = Number(row?.quantity   || 0);
      if (!pollLtp || !qty) continue;
      // BUGFIX: prefix key by kind so same symbol in positions + holdings
      // for the same account doesn't collide ('P' vs 'H' namespace).
      const key = kind + '\x00' + sym + '\x00' + String(row?.account || '');
      m.set(key, (live - pollLtp) * qty);
    }
  }

  const _liveDeltaByRow = $derived.by(() => {
    /** @type {Map<string, number>} */
    const m = new Map();
    // Read _throttledTick so the derived re-runs at 4 Hz max — not on
    // every SvelteMap entry change (which fires per-tick). getSnapshot
    // calls inside _addDeltaEntries are wrapped in untrack so they don't
    // register per-symbol reactive deps that would defeat the throttle.
    // Same scheduler-pressure fix BH6 applied to MarketPulse buildUnified.
    void _throttledTick;
    // Read _mktTick so the derived re-runs on the market open/close
    // boundary (otherwise we'd wait up to 30s for _load to pick it up).
    void _mktTick;
    // Gate the delta per exchange: MCX rows during MCX hours (09:00–23:30
    // IST), NSE/BSE/NFO/BFO/CDS rows during NSE hours (09:15–15:30 IST).
    const nseOpen = isNseOpen();
    const mcxOpen = isMcxOpen();
    const appliesToRow = (/** @type {any} */ row) => {
      const exch = String(row?.exchange || '').toUpperCase();
      if (exch === 'MCX') return mcxOpen;
      return nseOpen;
    };
    _addDeltaEntries(m, positions, 'P', appliesToRow);
    _addDeltaEntries(m, holdings,  'H', appliesToRow);
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
  // P pill slots 1 + 2: ALL positions (no exchange filter), matching the
  // MarketPulse positions TOTAL row (gold standard SSOT). Includes NSE/BSE
  // equity intraday positions alongside F&O so the P pill stays in sync
  // with the Pulse positions total on every page.
  // baseDayPnlForPosition applies the new-position override (oq=0 → pnl).
  //
  // NO SSE delta here — MarketPulse TOTAL explicitly uses broker snapshot
  // `_broker_pnl` (= Σ r.pnl) without a live-tick delta so both surfaces
  // stay in sync. Adding a delta to slot 2 (lifetime P&L) would diverge from
  // the TOTAL row whenever SSE ticks arrive between polls, which is the root
  // cause of the operator-reported slot-2 inconsistency. Slot 1 (day P&L)
  // carries the live signal via dispPositionsToday which IS delta-corrected.
  const _livePositionsPnl = $derived.by(() => {
    let pnlTotal = 0;
    for (const p of positions) pnlTotal += Number(p?.pnl || 0);
    return pnlTotal;
  });
  const _livePositionsToday = $derived.by(() => {
    // Gate: re-run at 4 Hz max via the throttled-tick mirror. getSnapshot
    // calls below are wrapped in untrack() so they don't register
    // per-symbol reactive deps that would defeat the throttle — same
    // pattern as _liveDeltaByRow and candidatesDayPnl on the derivatives
    // page. The throttle ensures the loop runs at most 4 times/sec even
    // during a burst of SSE ticks for a large position book.
    void _throttledTick;
    const _mktOpen = isNseOpen() || isMcxOpen();
    let dayTotal = 0;
    for (const p of positions) {
      const sym = String(p?.tradingsymbol || '').toUpperCase();
      // Live tick — use untrack so we don't register per-symbol reactive
      // deps (the _throttledTick gate above already controls re-derive cadence).
      const liveLtp = _mktOpen ? untrack(() => getSnapshot(sym)?.ltp) : null;
      dayTotal += livePositionDayPnl(
        {
          closePx: Number(p?.close_price ?? 0),
          pollLtp: Number(p?.last_price ?? 0),
          qty:     Number(p?.quantity ?? 0),
          avg:     Number(p?.average_price ?? 0),
          dcvRow:  p,
        },
        liveLtp ?? null,
        { marketOpen: _mktOpen },
      );
    }
    return dayTotal;
  });
  const _liveHoldingsToday = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.day_change_val || 0) + _delta(h, 'H');
    return s;
  });
  // NO SSE delta on _liveHoldingsTotal / _liveHoldingsValue — mirrors the
  // same rationale as _livePositionsPnl: MarketPulse Holdings TOTAL uses
  // _broker_pnl (= Σ r.pnl) and liveHold-recomputed cur_val per-row, but
  // the TOTAL row falls back to broker snapshot for cross-surface sync. Pure
  // broker-snapshot sums here keep H pill slots 2 + 3 consistent with the
  // Holdings grid TOTAL.
  const _liveHoldingsTotal = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.pnl || 0);
    return s;
  });
  const _liveHoldingsValue = $derived.by(() => {
    let s = 0;
    for (const h of holdings)  s += Number(h?.cur_val || 0);
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
    // During market open, suppress the live-derived assignment until a
    // fresh poll cycle (one completed AFTER the open transition) lands —
    // otherwise positions[].day_change_val is stale from yesterday.
    if (open && _pollCycleStamp <= _openTransitionStamp) return;
    // Always mirror the live derived. During open hours it tracks SSE
    // ticks live; during closed hours it equals Σ snapshot.day_pnl —
    // the LAST in-session P&L per the market-close-snapshot rule. The
    // prior localStorage freeze dance went stale when the snapshot
    // writer logged zeros (auth outage) and never recovered — operator
    // saw 0.0 instead of the real ₹84k positions P∆. The snapshot is
    // now the SSOT so we read from it directly.
    // Guard: only overwrite dispPositionsToday when the live derived is
    // meaningful — prevents a zero flash during the brief live→snapshot
    // gap at market close when positions briefly clear before the snapshot
    // arrives. The last non-zero value from the in-session poll is retained.
    if (positions.length > 0 || _livePositionsToday !== 0) {
      dispPositionsToday = _livePositionsToday;
    }
    if (holdings.length > 0 || _liveHoldingsToday !== 0) {
      dispHoldingsToday = _liveHoldingsToday;
    }
    if (!open) return;
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

  // Expiry profit — F&O positions only (futures + options), excludes equity.
  // "What would I make/lose if every open derivative position expired RIGHT NOW
  //  at the current spot?" — useful for understanding max-risk at expiry.
  //
  // Math:
  //   Futures:      (live_ltp − avg_cost) × qty
  //   Long CE:      max(spot − strike, 0) × qty − avg × qty
  //   Short CE:     avg × |qty| − max(spot − strike, 0) × |qty|
  //   PE symmetric.  [qty is signed: positive = long, negative = short;
  //                   formula (intrinsic − avg) × qty handles both signs]
  //
  // Spot source: symbolStore snapshot keyed by the RESOLVED tradingsymbol
  // (e.g. "NIFTY 50" for NIFTY options, "GOLD26JUNFUT" for MCX GOLD options).
  // inst.u gives the underlying root name ("NIFTY", "GOLD") — resolveUnderlying
  // translates that to the correct tradeable tradingsymbol stored in symbolStore.
  // Spots are pre-fetched by _loadUnderlyingSpots() on each poll cycle.
  // If no live snapshot exists for an option's underlying we skip that leg
  // (contribute 0) rather than feeding the option's own LTP as a proxy —
  // doing so would compute max(300 − 22000, 0) = 0 (wrong for deep ITM)
  // or huge phantom intrinsic.
  //
  // Gated by _throttledTick (4 Hz) not per-SSE-tick to avoid scheduler
  // pressure; matches the same throttle already used by _liveDeltaByRow.

  /** Returns true when the exchange is a derivative segment (not equity). */
  function _isDerivativeExch(/** @type {string} */ exch) {
    return ['NFO', 'MCX', 'CDS', 'BFO'].includes(exch);
  }

  /**
   * Step 1: try the backend-stamped underlying_ltp (SSOT, always preferred).
   * Step 2: symbolStore snapshot chain for resolved tradingsymbol / root / inst.u.
   * Step 3: row-scan of positions + holdings for a matching last_price.
   * Returns the spot price, or 0 when none found.
   *
   * @param {any} p - raw position row (for underlying_ltp)
   * @param {string} root - underlying root name (e.g. "NIFTY", "GOLD")
   * @param {any} inst - instrument record from instruments cache
   * @param {any} resolved - result of resolveUnderlying()
   * @param {any[]} posRows - positions array for row-scan fallback
   * @param {any[]} holdRows - holdings array for row-scan fallback
   * @returns {number}
   */
  function _resolveOptionSpot(p, root, inst, resolved, posRows, holdRows) {
    // SSOT: backend stamps underlying_ltp on each option row (positions.py
    // _enrich_positions Pass 3). Prefer this — it's what Greeks / IV used,
    // and it's populated even during closed hours via LKG cache.
    let spot = Number(p?.underlying_ltp || 0);
    if (spot > 0) return spot;

    // Fallback chain — demo/sim mode + legacy payloads without underlying_ltp.
    // Same 4 steps as derivatives/+page.svelte:_rootSpot().
    for (const key of [resolved?.tradingsymbol, root, inst?.u].filter(Boolean)) {
      const v = untrack(() => getSnapshot(String(key).toUpperCase())?.ltp);
      if (typeof v === 'number' && v > 0) { spot = v; break; }
    }
    if (spot > 0) return spot;

    // Row-scan fallback: check positions then holdings for a matching symbol.
    const wantKey  = String(resolved?.tradingsymbol || root).toUpperCase();
    const wantRoot = String(root).toUpperCase();
    for (const src of [posRows, holdRows]) {
      if (spot > 0) break;
      for (const _row of (src ?? [])) {
        const row = /** @type {any} */ (_row);
        const rSym = String(row?.symbol || row?.tradingsymbol || '').toUpperCase();
        if (!rSym) continue;
        if (rSym === wantKey || rSym === wantRoot) {
          const lp = Number(row?.last_price || 0);
          if (lp > 0) { spot = lp; break; }
        }
      }
    }
    return spot;
  }

  /**
   * Compute expiry P&L for one option leg.
   * Returns the contribution (number) or null when spot cannot be resolved.
   *
   * @param {any} p - raw position row
   * @param {string} sym - tradingsymbol (upper-cased)
   * @param {number} qty - signed quantity
   * @param {number} avg - average price
   * @param {any[]} posRows - positions for spot row-scan fallback
   * @param {any[]} holdRows - holdings for spot row-scan fallback
   * @returns {number | null}
   */
  function _optionLegExpiryPnl(p, sym, qty, avg, posRows, holdRows) {
    // Resolve underlying root — regex-first so cold instruments cache
    // doesn't gate the compute.
    const decomp = decomposeSymbol(sym);
    const inst   = getInstrument(sym);
    const root   = decomp.root || inst?.u || null;
    if (!root) return null;

    const resolved = resolveUnderlying(root, findNearestFuture);
    const spot = _resolveOptionSpot(p, root, inst, resolved, posRows, holdRows);
    if (!(spot > 0)) return null;

    // SHARED SSOT — same helper the derivatives page uses.
    return expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'opt' }, spot);
  }

  /**
   * Compute expiry P&L for one futures leg.
   * Returns the contribution (number) or null when LTP is unavailable.
   *
   * @param {any} p - raw position row
   * @param {string} sym - tradingsymbol (upper-cased)
   * @param {number} qty - signed quantity
   * @param {number} avg - average price
   * @returns {number | null}
   */
  function _futureLegExpiryPnl(p, sym, qty, avg) {
    // Future spot = its own LTP; use shared helper for parity.
    const live = untrack(() => getSnapshot(sym)?.ltp) || Number(p?.last_price || 0);
    if (!(live > 0)) return null;
    return expiryPnl({ symbol: sym, qty, avg_cost: avg, kind: 'fut' }, live);
  }

  const _expiryProfit = $derived.by(() => {
    void _throttledTick;
    void _mktTick;
    let total = 0;
    for (const p of positions) {
      const exch = String(p?.exchange || '').toUpperCase();
      if (!_isDerivativeExch(exch)) continue;
      const sym  = String(p?.tradingsymbol || '').toUpperCase();
      const qty  = Number(p?.quantity) || 0;
      const avg  = Number(p?.average_price) || 0;
      if (!qty) {
        // Closed leg — realized P&L is locked in, no spot needed
        total += Number(p?.pnl || 0);
        continue;
      }
      const isCE  = sym.endsWith('CE');
      const isPE  = sym.endsWith('PE');
      const isFut = sym.endsWith('FUT') || (!isCE && !isPE && exch !== 'CDS');
      let v = null;
      if (isCE || isPE) {
        v = _optionLegExpiryPnl(p, sym, qty, avg, positions, holdings);
      } else if (isFut) {
        v = _futureLegExpiryPnl(p, sym, qty, avg);
      }
      if (v != null) total += v + Number(p?.realised || 0);
    }
    return total;
  });
  // Margin: available (what's deployable) and total (used + avail = full
  // capacity). Pill shows avail / total to match the operator's mental
  // model "what room do I have, out of what I'd have if everything
  // unlocked". Util % is no longer surfaced — total clarifies the same
  // signal without needing a percentage.
  const marginAvail = $derived.by(() => {
    let s = 0;
    for (const f of funds) s += Number(f?.avail_margin || 0);
    return s;
  });
  const marginTotal = $derived.by(() => {
    let s = 0;
    for (const f of funds) {
      s += Number(f?.used_margin  || 0);
      s += Number(f?.avail_margin || 0);
    }
    return s;
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
      flash.update('PE',   _expiryProfit);
      flash.update('M',    marginAvail);
      flash.update('Mt',   marginTotal);
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
  //
  // Gate: skip the heartbeat when both markets are closed. The book
  // poller still fires during closed hours (intentionally, to refresh
  // snapshot / cash / margin data) but no live LTP ticks are arriving.
  // Pulsing the strip during closed hours misleads the operator into
  // thinking live prices are updating. _mktTick is a $state that flips
  // on every session-boundary tick from the visibleInterval above,
  // so the effect re-evaluates automatically at market open/close.
  let _heartbeatOn = $state(false);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _heartbeatTimer = null;
  $effect(() => {
    if (_pollCycleStamp === 0) return;  // skip mount paint
    if (_mktTick === 0) return;         // both markets closed — slate pulse handles it
    _heartbeatOn = true;
    if (_heartbeatTimer) clearTimeout(_heartbeatTimer);
    // 300ms — unified duration (tick-bus synchrony). Previously 450ms.
    _heartbeatTimer = setTimeout(() => {
      _heartbeatOn = false;
      _heartbeatTimer = null;
    }, 300);
  });

  // Closed-hours poll pulse — dim slate border flash when both markets are
  // closed but the background poller still refreshes broker data (positions,
  // holdings, funds, margins, settlement updates). Distinct from the amber
  // heartbeat: slate palette signals "data refreshed, no live ticks."
  let _pollPulseOn = $state(false);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _pollPulseTimer = null;
  $effect(() => {
    if (_pollCycleStamp === 0) return;  // skip mount paint
    if (_mktTick !== 0) return;         // at least one market open — amber handles it
    _pollPulseOn = true;
    if (_pollPulseTimer) clearTimeout(_pollPulseTimer);
    _pollPulseTimer = setTimeout(() => {
      _pollPulseOn = false;
      _pollPulseTimer = null;
    }, 300);
  });

  // Tick-bus border shimmer — per-tick sky-300 border flash driven by
  // real SSE ticks (separate from the poll-based amber heartbeat).
  // Direction not applicable on this surface; sky (neutral) palette per
  // the unified spec. 300ms + cubic-bezier(0.4,0,0.2,1) — same as all
  // other flash surfaces.
  // Uses the same a/b class-toggle pattern as RefreshButton: each emit
  // alternates between ps-tick-border-a and ps-tick-border-b (distinct
  // @keyframes names) so the browser restarts the animation on every
  // tick even during continuous bursts, rather than sitting static.
  let _tickBorderClass = $state(/** @type {'' | 'ps-tick-border-a' | 'ps-tick-border-b'} */ (''));
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _tickBorderTimer = null;
  // Leading-edge throttle timestamp (performance.now() units). Ticks that arrive
  // before this time are ignored; resets 1000ms after each leading-edge fire.
  // Plain number (not $state) — no reactive dep needed, just a gate check.
  let _tickBorderThrottleUntil = 0;
  /** @type {(() => void) | null} */
  let _tickBusUnsub = null;
</script>

<div class={'ps-strip' + (_heartbeatOn ? ' ps-heartbeat' : '') + (_pollPulseOn ? ' ps-poll-pulse' : '') + (_tickBorderClass ? ' ' + _tickBorderClass : '') + (_staleFailCount >= 2 ? ' ps-stale' : '')}>
  <span class="ps-agg" title="P — Day P&L / Lifetime P&L / F&amp;O expiry P&L (all accounts, all exchanges)">
    <span class="ps-agg-k">P</span>
    <span class={'ps-agg-v ' + (dispPositionsToday > 0 ? 'ps-pos' : dispPositionsToday < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Pd')}
      style="cursor:pointer"
      role="button"
      tabindex="0"
      title="Click for Day P&L breakup"
      onclick={() => _dayPnlBreakupOpen = true}
      onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && (_dayPnlBreakupOpen = true)}
      >{fmtMoney(dispPositionsToday)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ' + (_livePositionsPnl > 0 ? 'ps-pos' : _livePositionsPnl < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('P')}
      >{fmtMoney(_livePositionsPnl)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ps-exp ' + flash.classOf('PE')}
      >{fmtMoney(_expiryProfit)}</span>
  </span>
  <!-- Margin pill: available / total (used + avail). Operator wants the
       "room I have / full capacity" framing rather than util %. -->
  <span class="ps-agg" title="Margin: available / total (used + avail)">
    <span class="ps-agg-k">M</span>
    <span class={'ps-agg-v ' + (marginAvail > 0 ? 'ps-cash' : marginAvail < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('M')}
      >{fmtMoney(marginAvail)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ' + (marginTotal > 0 ? 'ps-cash' : marginTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Mt')}
      >{fmtMoney(marginTotal)}</span>
  </span>
  <!-- Cash pill: available (CA, deployable now) / total (incl. premium
       tied up in long options). Avail-first matches the M pill's
       framing. Canonical labels (Bloomberg PRTU, IBKR Portfolio):
         CA = Cash Available — nets realised P&L, premium debits,
              blocked margin per broker books.
         C  = Total Cash    = CA + Σ(avg × qty for long CE/PE)
       Per-broker drift in how realised M2M is folded into avail.cash
       is documented in the audit memo; if the sum diverges from broker
       apps, the Dhan/Groww adapter math is the first place to look. -->
  <span class="ps-agg" title="Cash: available (CA, deployable now) / total (C, incl. premium tied up in long options)">
    <span class="ps-agg-k">C</span>
    <span class={'ps-agg-v ' + (liveCashTotal > 0 ? 'ps-cash' : liveCashTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Cash')}
      >{fmtMoney(liveCashTotal)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ' + (cashTotal > 0 ? 'ps-cash' : cashTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Cp')}
      >{fmtMoney(cashTotal)}</span>
  </span>
  <span class="ps-agg" title="Holdings: today's MTM move / current value / lifetime P&L">
    <span class="ps-agg-k">H</span>
    <span class={'ps-agg-v ' + (dispHoldingsToday > 0 ? 'ps-pos' : dispHoldingsToday < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('HDd')}
      >{fmtMoney(dispHoldingsToday)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ps-cash ' + flash.classOf('H')}>{fmtMoney(_liveHoldingsValue)}</span
    ><span class="ps-agg-sep">/</span
    ><span class={'ps-agg-v ' + (_liveHoldingsTotal > 0 ? 'ps-pos' : _liveHoldingsTotal < 0 ? 'ps-neg' : 'ps-flat') + ' ' + flash.classOf('Hd')}
      >{fmtMoney(_liveHoldingsTotal)}</span>
  </span>
  <DayPnlBreakup
    open={_dayPnlBreakupOpen}
    {positions}
    onClose={() => (_dayPnlBreakupOpen = false)}
  />
</div>

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
    gap: 0.6rem;
    height: 1.5rem;
    box-sizing: border-box;
    padding: 0 0.5rem;
    /* Slice AT audit fix — was `#0a1020 → #131c33`, an off-token
       second stop. Anchor both stops on the chrome elevation tokens
       so the band reads as a sibling of navbar + footer. */
    background: linear-gradient(180deg, var(--algo-bg-elev1) 0%, #0f1828 100%);
    border-bottom: 1px solid var(--algo-amber-border-soft);
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    letter-spacing: 0.02em;
    text-decoration: none;
    user-select: none;
    transition: background 0.08s, border-bottom-color 0.18s;
  }
  /* Heartbeat — fires on every successful 30s poll completion via a
     class toggle that resolves after 300ms (unified tick-bus duration).
     Subtle amber-bordered glow that reads as "data refreshed" without
     competing with the per-cell directional flash. Operator: "I don't
     see any animation refreshing nav strip". */
  .ps-strip.ps-heartbeat {
    border-bottom-color: rgba(251, 191, 36, 0.85);
    background: linear-gradient(180deg, #0a1020 0%, #1a2640 100%);
  }
  /* Stale-data indicator — amber tint when positions or holdings have
     returned 2+ consecutive errors. Color-codes the strip without an
     intrusive banner message. */
  .ps-strip.ps-stale {
    background: linear-gradient(180deg, #1a1200 0%, #1a1500 100%);
    border-bottom-color: rgba(251, 146, 60, 0.6);
  }
  /* Closed-hours poll pulse — dim slate border flash when both markets are
     closed but broker data still refreshes (positions, holdings, funds,
     margins, settlement). Distinct from the amber heartbeat: slate signals
     "data refreshed, no live ticks." Same 300ms gate as the heartbeat. */
  .ps-strip.ps-poll-pulse {
    border-bottom-color: rgba(126, 151, 184, 0.60);
    background: linear-gradient(180deg, #0a1020 0%, #141c2e 100%);
  }

  /* Tick-border shimmer — per-SSE-tick sky-300 border flash driven by
     tickBus. Neutral (no direction). 300ms cubic-bezier(0.4,0,0.2,1)
     easing matches the unified animation spec.
     Uses the same a/b name-toggle pattern as RefreshButton: each emit
     alternates between ps-tick-border-a and ps-tick-border-b, which have
     DISTINCT @keyframes names. The browser sees an animation-name change
     and restarts the animation even during continuous tick bursts, giving
     a per-tick pulse rather than a static lit border. */
  .ps-strip.ps-tick-border-a {
    animation: ps-tick-border-kf-a 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .ps-strip.ps-tick-border-b {
    animation: ps-tick-border-kf-b 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }
  @keyframes ps-tick-border-kf-a {
    0%   { border-bottom-color: rgba(125, 211, 252, 0.70); }
    100% { border-bottom-color: rgba(125, 211, 252, 0); }
  }
  @keyframes ps-tick-border-kf-b {
    0%   { border-bottom-color: rgba(125, 211, 252, 0.70); }
    100% { border-bottom-color: rgba(125, 211, 252, 0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .ps-strip.ps-heartbeat { transition: none; }
    .ps-strip.ps-poll-pulse { transition: none; }
    .ps-strip.ps-tick-border-a,
    .ps-strip.ps-tick-border-b { animation: none; }
  }
  .ps-strip:hover {
    background: linear-gradient(180deg, #0a1020 0%, #1a2746 100%);
  }

  .ps-agg {
    display: inline-flex;
    align-items: baseline;
    gap: 0.2rem;
  }
  .ps-agg-k {
    color: var(--algo-muted);
    font-size: var(--fs-xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.02em;
  }
  .ps-agg-v {
    font-size: var(--fs-lg);
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

  .ps-pos  { color: var(--c-long); }
  .ps-neg  { color: var(--c-short); }
  .ps-flat { color: var(--algo-slate); }
  /* Negative cash (margin debt) flips to red via .ps-neg. */
  .ps-cash { color: #7dd3fc; }
  /* Expiry profit — amber action palette; signals a time-bound outcome. */
  .ps-exp  { color: var(--c-action); }
  @media (max-width: 640px) {
    /* Four pills (P · M · C · H) fill the mobile viewport width:
       P locks to the left edge; M / C / H distribute across the
       remaining width via `justify-content: space-between`. The
       inter-pill gap expands / contracts as the viewport resizes.
       Horizontal scroll retained as a last-resort safety net if a
       pill's trio grows past its column (long lifetime P&L numbers). */
    .ps-strip   { justify-content: space-between;
                  gap: 0.15rem; padding: 0 0.25rem;
                  overflow-x: auto; -webkit-overflow-scrolling: touch;
                  scrollbar-width: none; }
    .ps-strip::-webkit-scrollbar { display: none; }
    .ps-agg     { gap: 0.15rem; flex-shrink: 0; }
    .ps-agg-k   { font-size: var(--fs-2xs); }
    .ps-agg-v   { font-size: var(--fs-sm); }
  }
  @media (max-width: 380px) {
    /* Narrowest phones — drop one more notch. */
    .ps-strip   { padding: 0 0.2rem; }
    .ps-agg-k   { font-size: var(--fs-2xs); }
    .ps-agg-v   { font-size: var(--fs-xs); }
  }
</style>
