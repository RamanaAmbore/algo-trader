<!--
  RefreshButton — small icon button that triggers an on-demand refresh.
  Sized + palette-matched to FullscreenButton so the two sit cleanly
  side-by-side in any card header (refresh icon FIRST, fullscreen
  SECOND by convention — refresh is invoked more often).

  Usage:
    <RefreshButton onClick={() => loadData()} {loading} label="positions" />

  Connection-status badge
  -----------------------
  Subscribes to the global `connStatus` store ($lib/stores). When the
  store reports any broker accounts (`total > 0`), a small badge in the
  button's top-right corner shows the loaded count. Color tracks health:

     loaded === total   → green  (all broker accounts connected)
     0 < loaded < total → amber  (partial)
     loaded === 0       → red    (none loaded, total > 0)
     total === 0        → no badge (no broker config / demo mode)

  Tooltip surfaces the full "N of M broker accounts loaded" message so
  the badge isn't ambiguous. Same right-corner placement + size as the
  unread badges on OrderNotifications / AgentNotifications — operator
  reads "small number in the corner = stateful indicator" consistently
  across every icon family.

  When `loading` is true the icon spins, the title flips to "Refreshing…"
  so screen readers + tooltip both reflect state, and the click handler is
  blocked (avoiding double-fires that would otherwise hit the broker
  twice while the first request was still in flight).
-->
<script>
  import { connStatus, startConnStatusPoller, lastRefreshAt, formatDualTz, visibleInterval, postHibernationRefiring } from '$lib/stores';
  import { isNseOpen, isMcxOpen } from '$lib/marketHours';
  import { symbolTickCount } from '$lib/data/symbolStore.svelte.js';
  import { bookPollerTick } from '$lib/data/marketDataStores.svelte.js';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { onMount, onDestroy, untrack } from 'svelte';

  /**
   * @typedef {object} RefreshOpts
   * @property {boolean} [skipLtp] - When true (RefreshButton's both-markets-
   *   closed path), consumers should route positions/holdings fetches
   *   with `?skip_ltp=1` so cash/margins/holdings refresh from the broker
   *   while LTPs stay frozen at the daily_book snapshot value. Consumers
   *   that don't care about this can ignore the arg (backward compatible).
   *
   * @typedef {object} Props
   * @property {(opts?: RefreshOpts) => void} onClick - Click handler that should
   *   trigger the refresh. Called with `{ skipLtp: true }` on both-markets-
   *   closed clicks, `{ skipLtp: false }` otherwise.
   * @property {boolean} [loading] - Spinner state; click is suppressed while true.
   * @property {string} [label] - aria-label suffix (e.g. "positions", "audit").
   */
  /** @type {Props} */
  let { onClick, loading = false, label = 'data' } = $props();

  // Market-state tick — recomputes isNseOpen / isMcxOpen every 30 s so
  // the button's palette tracks session boundaries automatically. The
  // three buckets the operator cares about: both open (full hours),
  // only MCX open (commodity-only window), market closed.
  // Uses visibleInterval: pauses when hidden, fires immediately on return.
  let _nseOpen = $state(isNseOpen());
  let _mcxOpen = $state(isMcxOpen());
  /** @type {(() => void) | null} */
  let _mktTimer = null;
  // Tick-pulse animation — fires the button's box-shadow halo at ~4Hz
  // whenever SSE ticks land in symbolStore. Operator: "bump refresh
  // to 4hz." Throttle to 250ms so a 100-tick/sec burst doesn't strobe
  // but the pulse reads as continuously alive during heavy flow. The
  // class toggles between `rf-tick-a`/`rf-tick-b` so CSS restarts the
  // animation on every pulse (re-applying the same class wouldn't
  // replay).
  let _tickPulseClass = $state(/** @type {'' | 'rf-tick-a' | 'rf-tick-b'} */ (''));
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _pulseTimer = null;
  /** @type {(() => void) | null} */
  let _pulseUnsub = null;
  // Poll-pulse — slow slate halo fired on every background broker-data poll
  // (positions, holdings, funds, margins). Fires regardless of market hours
  // so the operator can see that non-ticker data is still refreshing when
  // both markets are closed. Distinct from the fast sky-blue tick-pulse:
  // 1.5s duration, slate palette, no SVG rotation (icon stays still).
  let _pollPulseClass = $state(/** @type {'' | 'rf-poll-a' | 'rf-poll-b'} */ (''));

  // Post-hibernation refire state — true while _exitHibernation() is running
  // its subscriber flush. Causes the button to spin via _showSpinning so the
  // operator sees an ambient signal that stale data is being refreshed.
  // Completely separate from `loading` (manual click spinner); both can be
  // active simultaneously without conflict.
  // Declared here (before any onMount / $derived that reference it) to avoid
  // the "used before its declaration" compile error in strict lexical order.
  let _refiring = $state(false);
  /** @type {(() => void) | null} */
  let _unsubRefiring = null;
  /** @type {(() => void) | null} */
  let _onRefreshKey = null;

  onMount(() => {
    // 1. Ensure the global connection-status poller is running. Idempotent —
    //    safe to call from every mounted RefreshButton.
    startConnStatusPoller();

    // 2. Market-state tick: recomputes isNseOpen / isMcxOpen every 30 s.
    const tick = () => {
      _nseOpen = isNseOpen();
      _mcxOpen = isMcxOpen();
    };
    _mktTimer = visibleInterval(tick, 30_000);

    // Keyboard shortcut `r` — layout dispatches a `refresh-page` custom
    // event on `window`; every mounted RefreshButton fires its own click
    // handler so the active page's load() runs. Multiple RefreshButtons
    // on the same page (e.g. /admin/derivatives has three) all fire, but
    // that is harmless — each drives a different data slice.
    // Stored as module-scoped var so onDestroy (below) can tear it down.
    _onRefreshKey = () => _handleClick();
    window.addEventListener('refresh-page', _onRefreshKey);

    // 3. Subscribe inside onMount (not $effect) so the subscription is
    //    registered ONCE on mount; the callback firing per tick does not
    //    re-run an $effect body and doesn't touch reactive state, so it
    //    cannot cascade scheduler work.
    //
    //    STUCK-SPINNER ROOT CAUSE (Jun 2026 reaudit): the per-tick rotation
    //    animation (`rf-tick-rotate`, 0.25s finite) used to share the SAME
    //    <svg> element with the loading-state animation (`rf-spin`, 0.9s
    //    infinite). The `animation` CSS property is a shorthand — applying
    //    it a second time RESETS the first. Cascade order put the tick-rotate
    //    rule AFTER the rf-spin rule, so on every SSE tick during a refresh
    //    the spinner would briefly do a 180° rotate-then-freeze cycle and
    //    appear stuck. Fix here: SKIP the per-tick toggle entirely while
    //    `loading` is true. The spinner itself is the busy signal — the
    //    tick-pulse cosmetic is meaningless during a manual refresh. The CSS
    //    also gains a defensive `:not(.rf-spinning)` guard on the tick-rotate
    //    selector so even if the class somehow leaked through, the spin
    //    animation would still win.
    _pulseUnsub = symbolTickCount.subscribe(() => {
      if (_pulseTimer) return;
      // Skip class toggle while spinner is active (loading OR refiring) —
      // see comment above. _refiring is a module-scope $state variable
      // so the closure captures the live value at the time the tick fires.
      if (loading || _refiring) return;
      // Skip tick-pulse animation when both markets are closed. Background
      // pollers (sparkline, snapshot refresh, performance) still emit on
      // symbolTickCount during closed hours and would falsely signal
      // "live ticks arriving" to the operator. _nseOpen / _mcxOpen are
      // module-scope $state vars refreshed every 30 s by the visibleInterval
      // in onMount, so the closure always captures the current session state.
      if (!_nseOpen && !_mcxOpen) return;
      // Skip class toggle during the post-loading cooldown window (800 ms
      // after spinner stops). The next SSE tick arriving right after a
      // manual refresh would otherwise trigger rf-tick-rotate and the
      // operator would perceive the button as "animating twice".
      if (_loadingExitAt && performance.now() < _loadingExitAt) return;
      _pulseTimer = setTimeout(() => {
        // Re-check at fire time — loading, _refiring, or market state may
        // have changed between subscribe and timer fire. Also re-check the
        // cooldown in case the 250 ms timer fires inside the 800 ms window.
        if (!loading && !_refiring && (_nseOpen || _mcxOpen) && !(_loadingExitAt && performance.now() < _loadingExitAt)) {
          _tickPulseClass = _tickPulseClass === 'rf-tick-a' ? 'rf-tick-b' : 'rf-tick-a';
        }
        _pulseTimer = null;
      }, 250);
    });

    // 4. Store subscriptions — tooltips, badge counts, refiring state.
    //    SUBSCRIPTION LEAK FIX (Perf audit Jul 2026): these used to be in a
    //    separate fourth onMount block; collapsed here so all teardowns and
    //    subscriptions share a single lifecycle anchor. All unsubs are paired
    //    in onDestroy below.
    _unsubLast = lastRefreshAt.subscribe((v) => { _lastTs = v || 0; });
    _unsubConn = connStatus.subscribe((v) => {
      _loaded = Number(v?.loaded) || 0;
      _total  = Number(v?.total)  || 0;
      _backendOk = v?.backendOk !== false; // default true
      _failingAccounts = Array.isArray(v?.failingAccounts) ? v.failingAccounts : [];
    });
    _unsubRefiring = postHibernationRefiring.subscribe((v) => { _refiring = v; });
  });

  // Combined spinner gate — true when either loading (manual click) OR
  // _refiring (post-hibernation flush) is active. Used everywhere the
  // template previously used `loading` to decide which glyph to show
  // and which CSS class to apply.
  //
  // Max-spin watchdog — if the parent's `loading` prop stays true for
  // more than 20 s (broker call hanging, no timeout on fetch, etc.),
  // stop showing spin visually. Prevents the "continuously rotates"
  // regression when Kite is slow/502-ing. loading itself is not
  // touched — parent owns that state — only the visible feedback
  // times out. Operator: "refresh button after click continuously
  // rotates."
  const _MAX_SPIN_MS = 20_000;
  let _spinCapExpired = $state(false);
  let _spinCapTimer   = /** @type {ReturnType<typeof setTimeout> | null} */ (null);
  $effect(() => {
    if ((loading || _refiring) && !_spinCapExpired) {
      if (_spinCapTimer == null) {
        _spinCapTimer = setTimeout(() => { _spinCapExpired = true; }, _MAX_SPIN_MS);
      }
    } else {
      // loading cleared (or already expired) — reset for the next click.
      if (_spinCapTimer != null) { clearTimeout(_spinCapTimer); _spinCapTimer = null; }
      if (!loading && !_refiring) _spinCapExpired = false;
    }
  });
  const _showSpinning = $derived((loading || _refiring) && !_spinCapExpired);

  // Refire-only flag — true when post-hibernation refire is active but
  // no manual click is in flight. This drives the violet palette on the
  // spinner SVG so the operator can distinguish "auto reconnect after
  // idle" (violet) from "manual Refresh click" (cyan-400).
  const _refireOnly = $derived(_refiring && !loading);

  // Belt-and-suspenders: when spinner engages (via either source), clear any
  // residual tick-pulse or poll-pulse class so neither animation can
  // override the spin animation via cascade.
  $effect(() => {
    if (_showSpinning) {
      untrack(() => {
        if (_tickPulseClass !== '') _tickPulseClass = '';
        if (_pollPulseClass !== '') _pollPulseClass = '';
      });
    }
  });

  // Poll-pulse — fires on every bookPollerTick (background position/holdings/
  // funds/margin poll, ~5 min cadence). Always active: during market hours the
  // slow slate halo sits behind the fast sky-blue tick-pulse; during closed
  // hours it's the only active signal, showing that broker data still refreshes.
  $effect(() => {
    void bookPollerTick.value;
    if (_showSpinning) return;
    untrack(() => {
      _pollPulseClass = _pollPulseClass === 'rf-poll-a' ? 'rf-poll-b' : 'rf-poll-a';
    });
  });

  // Post-loading cooldown — after the spinner stops (loading true → false),
  // suppress rf-tick-rotate for 800 ms so the next incoming SSE tick doesn't
  // trigger a second animation pulse that the operator perceives as the
  // refresh button "animating twice". The position-strip freshness shimmer
  // still fires (that's the operator's signal data arrived); only the button
  // stays calm during the cooldown window.
  //
  // Implementation: _loadingExitAt holds performance.now() + 800 while the
  // cooldown is active, or 0 when idle. The symbolTickCount subscriber checks
  // this value (not reactive state, just a plain number) to skip the pulse.
  // A scheduled clearance resets it after the window expires so it doesn't
  // stay armed across later natural SSE ticks.
  let _loadingExitAt = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _cooldownTimer = null;
  let _prevSpinningForCooldown = false;
  $effect(() => {
    const wasSpinning = _prevSpinningForCooldown;
    _prevSpinningForCooldown = _showSpinning;
    if (wasSpinning && !_showSpinning) {
      // Spinner just stopped (either manual click finished OR hibernation
      // refire completed); arm the 800 ms cooldown so the very next SSE
      // tick doesn't immediately trigger rf-tick-rotate.
      if (_cooldownTimer) clearTimeout(_cooldownTimer);
      _loadingExitAt = performance.now() + 800;
      _cooldownTimer = setTimeout(() => {
        _loadingExitAt = 0;
        _cooldownTimer = null;
      }, 800);
    }
  });
  onDestroy(() => {
    _mktTimer?.();   // visibleInterval teardown (stops the interval + removes listener)
    if (_pulseTimer) clearTimeout(_pulseTimer);
    if (_cooldownTimer) clearTimeout(_cooldownTimer);
    _pulseUnsub?.();
    _unsubLast?.();
    _unsubConn?.();
    _unsubRefiring?.();
    if (_onRefreshKey) window.removeEventListener('refresh-page', _onRefreshKey);
  });

  // Palette class — drives the three-bucket colour swap on the button.
  //   rf-mkt-both    → emerald  (Equity + MCX both open)
  //   rf-mkt-mcx     → amber    (only MCX, e.g. 15:30–23:30 IST)
  //   rf-mkt-closed  → slate    (everything closed)
  const _mktClass = $derived(
    _nseOpen && _mcxOpen ? 'rf-mkt-both'
    : _mcxOpen            ? 'rf-mkt-mcx'
    :                       'rf-mkt-closed'
  );
  // Tooltip suffix surfaces which segments are open so the colour
  // change is self-explanatory the first time the operator sees it.
  // Both-closed variant surfaces the "only cash/margins refreshes" caveat
  // per the per-exchange close-snapshot lifecycle (Jul 2026): the click
  // still fires but the fetch chain passes `?skip_ltp=1` to the book
  // endpoints so the broker isn't hit for LTPs.
  const _bothClosed = $derived(!_nseOpen && !_mcxOpen);
  const _mktTooltip = $derived(
    _nseOpen && _mcxOpen ? 'Equity + MCX open'
    : _mcxOpen            ? 'MCX open · Equity closed'
    :                       'Markets closed — refresh only updates cash/margins/holdings'
  );
  // Closed-market UX (slice Market-Lifecycle).
  //
  // Previous behaviour: click during closed-market → modal popup,
  // refresh BLOCKED ("Both NSE and MCX are currently closed. OK").
  // Operator now wants the refresh to STILL FIRE — broker positions,
  // cash, holdings still refresh from the daily_book / snapshot path —
  // and a brief toast surfaces the "you're looking at a close snapshot"
  // context. No more blocking popup. The toast is auto-dismissed by
  // toastStore's 3000ms default.
  //
  // The toast says when the markets next reopen so the operator knows
  // when live ticks will resume. Equity opens 09:15 IST, MCX 09:00 IST;
  // we surface MCX (earlier of the two) as the next-open window.
  let _showClosedNotice = $state(false); // retained for back-compat popups
  function _nextOpenLabel() {
    // Reopen-time hint: which window comes next?
    //   NSE 09:15-15:30 — equity
    //   MCX 09:00-23:30 — commodities
    // Outside both, MCX opens earliest (09:00 IST) on the next trading
    // day. We don't compute the holiday-aware next-trading-day here —
    // the server-side market status poller already gates polls; this
    // toast just gives a quick human cue.
    return '09:00 IST';
  }
  function _handleClick() {
    if (_showSpinning) return;
    if (!_nseOpen && !_mcxOpen) {
      // Fire the refresh anyway — broker positions / cash / holdings
      // come from the snapshot path during closed hours and the
      // operator may still want fresher numbers (e.g. after a manual
      // fund transfer). The toast clarifies why ticks stay frozen.
      // Per-exchange close-snapshot lifecycle (Jul 2026): pass
      // { skipLtp: true } to parents that opted in — the fetch chain
      // then flips to `?skip_ltp=1` and the broker is NOT called for
      // LTP data. Callers that ignore the arg fall through to their
      // legacy no-arg refresh flow (backward compatible).
      try {
        toast.info(
          `Showing close snapshot — markets reopen at ${_nextOpenLabel()}`,
          { timeoutMs: 3000 }
        );
      } catch (_) { /* toast store unavailable — silent */ }
      queueMicrotask(() => {
        try { onClick?.({ skipLtp: true }); }
        catch (e) { console.warn('[refresh] onClick threw:', e); }
      });
      return;
    }
    // CLICK-FEEDBACK FIX (Perf audit Jul 2026): defer the parent's
    // onClick by one microtask so the click handler returns immediately,
    // letting the browser paint the disabled / spinner state before any
    // synchronous work the parent kicks off (Promise.allSettled wiring,
    // legsKey signature compute, etc.). Operator: "refresh button
    // getting stuck still is an issue" — the stall was the gap between
    // the click and the next paint when the parent's onClick body did
    // tens of ms of bookkeeping synchronously before the first await.
    queueMicrotask(() => {
      try { onClick?.({ skipLtp: false }); }
      catch (e) { console.warn('[refresh] onClick threw:', e); }
    });
  }

  // Watch the `loading` prop for true → false transitions and stamp
  // `lastRefreshAt`. Catches BOTH manual clicks (operator hits the
  // button) AND auto-refresh — as long as the page's load() sets
  // loading=true at the start and loading=false in finally, the
  // tooltip timestamp updates without any per-page wiring.
  let _prevLoading = false;
  $effect(() => {
    if (_prevLoading && !loading) {
      lastRefreshAt.set(Date.now());
    }
    _prevLoading = loading;
  });

  let _prevRefiring = false;
  $effect(() => {
    if (_prevRefiring && !_refiring) lastRefreshAt.set(Date.now());
    _prevRefiring = _refiring;
  });

  // Subscribe for tooltip rendering.
  // SUBSCRIPTION LEAK FIX (Perf audit Jul 2026): both subscribes used to
  // be top-level (no cleanup) — every RefreshButton instance leaked one
  // lastRefreshAt + one connStatus subscriber for the page lifetime.
  // With 1-3 RefreshButtons per algo page and route transitions never
  // destroying them, the listener list grew unbounded; each tick of the
  // 15 s connStatus poller / each manual refresh fanned out work to
  // every dead consumer. Bind subscribes through onMount/onDestroy so
  // unmount tears them down.
  let _lastTs = $state(0);
  /** @type {(() => void) | null} */
  let _unsubLast = null;
  /** @type {(() => void) | null} */
  let _unsubConn = null;

  // Badge state derived from the store.
  let _loaded = $state(0);
  let _total  = $state(0);
  let _backendOk = $state(true);
  let _failingAccounts = $state(/** @type {string[]} */ ([]));

  // _showBadge / _badgeText / _badgeClass dropped — count is now in
  // the navbar broker-chip (slice AX). The tooltip below still
  // surfaces the full N/M + failing-accounts story so this button
  // remains the diagnostic surface; only the visible digit moved.

  // Multi-line native tooltip (newlines render as soft breaks in
  // every browser's title="…" popover). Encodes the FULL connection
  // story so the operator can diagnose without leaving the page:
  //
  //   Line 1 — action / connection state (`Refresh — N of M broker
  //            accounts loaded`, or `API unreachable — retrying…`
  //            when the backend is down, or `Refreshing…` mid-fetch).
  //   Line 2 — failing broker accounts list, only when some are down.
  //   Line 3 — Last refreshed: <dual-tz timestamp>, matches the
  //            page-header wall clock format.
  const _connTitle = $derived.by(() => {
    /** @type {string[]} */
    const lines = [];
    if (_showSpinning) {
      lines.push('Refreshing…');
    } else if (!_backendOk) {
      lines.push('API unreachable — retrying every 15s');
    } else if (_total === 0) {
      lines.push('Refresh now');
    } else {
      lines.push(`Refresh — ${_loaded} of ${_total} broker accounts loaded`);
    }
    // Failing-broker detail — surface the account codes so the
    // operator knows WHICH broker to investigate. Skip when backend
    // is down because the list may be stale anyway.
    if (_backendOk && _failingAccounts.length > 0) {
      lines.push(`Failed: ${_failingAccounts.join(', ')}`);
    }
    if (_lastTs) {
      lines.push(`Last refreshed: ${formatDualTz(new Date(_lastTs))}`);
    }
    lines.push(`Market: ${_mktTooltip}`);
    return lines.join('\n');
  });
</script>

<div class="rf-wrap">
<button
  type="button"
  class="rf-btn {_mktClass} {_tickPulseClass} {_pollPulseClass}"
  class:rf-spinning={_showSpinning}
  class:rf-refiring={_refireOnly}
  onclick={(e) => { e.stopPropagation(); _handleClick(); }}
  disabled={_showSpinning}
  aria-label={_refireOnly ? `Reconnecting ${label} after idle` : _showSpinning ? `Refreshing ${label}` : `Refresh ${label}`}
  title={_connTitle}>
  {#if _showSpinning}
    <!-- Loading state — distinct arc-spinner glyph (NOT the same
         refresh-arrow rotated). Reads as "working / fetching" rather
         than "refresh affordance". The arc spins via the rf-spinning
         keyframe below. Shown during both manual click (loading=true)
         and post-hibernation refire (_refiring=true). -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <circle cx="8" cy="8" r="5.5"
        fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round"
        stroke-dasharray="9 30" />
    </svg>
  {:else}
    <!-- Idle / refresh affordance — circular-arrow icon. -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" />
      <path d="M13.5 2v3.5H10"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  {/if}
  <!-- Badge moved to the navbar broker-chip (slice AX). The full
       connection story still lives in this button's native tooltip;
       the visible count digit was redundant once the navbar chip
       became ambient. Operator: "since 5/5 in navbar, we don't need
       to show the connection count on refresh button". -->
</button>

{#if _showClosedNotice}
  <!-- Click-outside catcher: a transparent fixed overlay closes the
       popup when the operator taps anywhere else. Lives at z-index just
       below the popup body so clicks on the popup itself stay live. -->
  <div class="rf-closed-overlay"
       role="presentation"
       onclick={(e) => { e.stopPropagation(); _showClosedNotice = false; }}></div>
  <div class="rf-closed-popup algo-modal" role="dialog" aria-modal="true"
       aria-label="Market closed notice"
       tabindex="-1"
       onclick={(e) => e.stopPropagation()}
       onkeydown={(e) => { if (e.key === 'Escape') { _showClosedNotice = false; } }}>
    <div class="rf-closed-title">Market closed</div>
    <div class="rf-closed-body">
      Both NSE and MCX are currently closed.
    </div>
    <div class="rf-closed-actions">
      <button type="button" class="rf-closed-btn rf-closed-ok"
              onclick={(e) => { e.stopPropagation(); _showClosedNotice = false; }}>
        OK
      </button>
    </div>
  </div>
{/if}
</div>

<style>
  /* Base button shape — palette comes from the .rf-mkt-* state class
     so the icon's hue tracks the live market session boundaries.
     Operator request: "give different color to refresh button based
     on if the market is open or not. it should use one color when
     equity and mcx open, only mcx open, market is closed."
       rf-mkt-both    → emerald  (Equity + MCX both open)
       rf-mkt-mcx     → amber    (only MCX, e.g. 15:30–23:30 IST)
       rf-mkt-closed  → slate    (everything closed)
     Card-control trio (Collapse / Fullscreen / DefaultSize) keeps
     cyan since they're card-scoped, not market-state indicators. */
  .rf-btn {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    margin: 0;
    border-radius: 3px;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
    overflow: visible;
    /* Defaults overridden by .rf-mkt-* below.
       Matches .rf-mkt-closed (slate) so any unclassified state
       degrades to the "inactive / no signal" tone rather than
       flashing emerald (the market-open colour). */
    background: rgba(126, 151, 184, 0.14);
    border: 1px solid rgba(126, 151, 184, 0.55);
    color: #94a3b8;
  }
  /* Both NSE + MCX open — emerald, the "full markets active" tone. */
  .rf-mkt-both {
    background: rgba(52, 211, 153, 0.14);
    border-color: rgba(52, 211, 153, 0.55);
    color: #34d399;
  }
  .rf-mkt-both:hover:not(:disabled) {
    background: rgba(52, 211, 153, 0.26);
    border-color: rgba(52, 211, 153, 0.85);
    color: #6ee7b7;
  }
  .rf-mkt-both:focus-visible {
    outline: 2px solid rgba(52, 211, 153, 0.65);
    outline-offset: 1px;
  }
  /* Only MCX — amber (matches the order-icon hue family + signals
     "commodities only", a partial-session state). */
  .rf-mkt-mcx {
    background: var(--c-action-14);
    border-color: var(--algo-amber-border);
    color: var(--c-action);
  }
  .rf-mkt-mcx:hover:not(:disabled) {
    background: var(--c-action-22);
    border-color: rgba(251, 191, 36, 0.85);
    color: #fcd34d;
  }
  .rf-mkt-mcx:focus-visible {
    outline: 2px solid rgba(251, 191, 36, 0.65);
    outline-offset: 1px;
  }
  /* Market closed — slate, the canonical "inactive / no signal" tone. */
  .rf-mkt-closed {
    background: rgba(126, 151, 184, 0.14);
    border-color: rgba(126, 151, 184, 0.55);
    color: #94a3b8;
  }
  .rf-mkt-closed:hover:not(:disabled) {
    background: rgba(126, 151, 184, 0.26);
    border-color: rgba(126, 151, 184, 0.85);
    color: var(--algo-slate);
  }
  .rf-mkt-closed:focus-visible {
    outline: 2px solid rgba(126, 151, 184, 0.65);
    outline-offset: 1px;
  }
  .rf-btn:disabled {
    cursor: progress;
    opacity: 0.85;
  }
  /* Spinner — bumped to (0,3,1) specificity via the duplicated `.rf-btn`
     selector so the loading-state animation ALWAYS wins over the
     tick-pulse rotate below (which is (0,2,1)). Previously the tick-rotate
     rule was defined later in this stylesheet with equal specificity,
     so on every SSE tick during a manual refresh the spinner would
     briefly do a 180° rotate-and-freeze cycle and read as "stuck"
     mid-animation. Operator-reported root cause (reaudit Jun 2026). */
  .rf-btn.rf-spinning.rf-spinning svg {
    animation: rf-spin 0.9s linear infinite !important;
    color: rgb(34, 211, 238); /* cyan-400 — manual Refresh */
  }
  /* Post-hibernation refire — amber-400 overrides the default cyan so
     the operator can see at a glance "tab is catching up after idle"
     vs "I clicked Refresh". Selector specificity (0,3,1) matches the
     base rf-spinning rule above; the rf-refiring qualifier pushes it
     to (0,4,1) so it wins without !important. */
  .rf-btn.rf-spinning.rf-refiring svg {
    color: rgb(139, 92, 246); /* violet-500 — post-hibernation refire (distinct from amber MCX-only resting state) */
  }
  @keyframes rf-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  /* Tick-pulse — two overlaid animations triggered when ticks land:
       1. Halo: sky-blue ring fades out over ~250ms (box-shadow)
       2. Rotation: icon rotates 180° smoothly each pulse so the button
          reads as "spinning" continuously during heavy SSE flow.
     Toggling between rf-tick-a / rf-tick-b restarts both each pulse.

     ANIMATION-RESTART BUG FIX (Jul 2026):
     The original rule used a SHARED animation name for both classes:
       .rf-btn.rf-tick-a, .rf-btn.rf-tick-b { animation: rf-tick-pulse ... }
     Per the CSS Animations spec, a running animation only restarts when
     the computed `animation-name` changes. Toggling a↔b while both
     point to the same keyframes name leaves the `animation-name` computed
     value UNCHANGED — so the browser never restarts the animation. The
     very first pulse fires (class '' → 'rf-tick-a' is a name change),
     but every subsequent a↔b toggle is a no-op. Operator sees: one pulse
     on page load, then nothing during continuous SSE tick flow.
     Fix: give each class a DISTINCT keyframes name (rf-tick-pulse-a /
     rf-tick-pulse-b, rf-tick-rotate-a / rf-tick-rotate-b). The a↔b
     toggle now changes the computed animation-name each cycle, which
     forces the browser to start a fresh animation sequence.

     The `:not(.rf-spinning)` guard ensures the rotate animation NEVER
     touches the spinner SVG while the button is in loading state — see
     rf-spin rule above for the full root-cause story. */
  /* Unified animation spec (2026-07 tick-bus synchrony):
     Duration 300ms, easing cubic-bezier(0.4,0,0.2,1) (material standard).
     Neutral sky-300 α 0.14 palette (no direction on the button). */
  .rf-btn.rf-tick-a {
    animation: rf-tick-pulse-a 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .rf-btn.rf-tick-b {
    animation: rf-tick-pulse-b 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .rf-btn.rf-tick-a:not(.rf-spinning) svg {
    animation: rf-tick-rotate-a 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .rf-btn.rf-tick-b:not(.rf-spinning) svg {
    animation: rf-tick-rotate-b 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }
  @keyframes rf-tick-pulse-a {
    0%   { box-shadow: 0 0 0 0 rgba(125, 211, 252, 0.55); background-color: rgba(125, 211, 252, 0.14); }
    100% { box-shadow: 0 0 8px 2px rgba(125, 211, 252, 0); background-color: transparent; }
  }
  @keyframes rf-tick-pulse-b {
    0%   { box-shadow: 0 0 0 0 rgba(125, 211, 252, 0.55); background-color: rgba(125, 211, 252, 0.14); }
    100% { box-shadow: 0 0 8px 2px rgba(125, 211, 252, 0); background-color: transparent; }
  }
  @keyframes rf-tick-rotate-a {
    from { transform: rotate(0deg); }
    to   { transform: rotate(180deg); }
  }
  @keyframes rf-tick-rotate-b {
    from { transform: rotate(0deg); }
    to   { transform: rotate(180deg); }
  }
  /* Poll-pulse — slow slate halo (1.5s) for background broker-data polls
     (positions / holdings / funds / margins). Always fires regardless of
     market hours: during open sessions the fast sky-blue tick-pulse dominates
     visually; during closed hours this is the only active signal. No SVG
     rotation — icon stays still so the operator reads "data refreshed"
     (slower, calmer) vs "live ticks arriving" (fast rotation).
     Distinct keyframe names (rf-poll-pulse-a / rf-poll-pulse-b) follow the
     same a/b toggle pattern as the tick-pulse so the browser restarts the
     animation on every poll cycle. */
  .rf-btn.rf-poll-a {
    animation: rf-poll-pulse-a 1.5s ease-out;
  }
  .rf-btn.rf-poll-b {
    animation: rf-poll-pulse-b 1.5s ease-out;
  }
  @keyframes rf-poll-pulse-a {
    0%   { box-shadow: 0 0 0 0 rgba(126, 151, 184, 0.50); }
    100% { box-shadow: 0 0 10px 3px rgba(126, 151, 184, 0); }
  }
  @keyframes rf-poll-pulse-b {
    0%   { box-shadow: 0 0 0 0 rgba(126, 151, 184, 0.50); }
    100% { box-shadow: 0 0 10px 3px rgba(126, 151, 184, 0); }
  }

  @media (prefers-reduced-motion: reduce) {
    .rf-btn.rf-spinning svg { animation: none; }
    .rf-btn.rf-spinning.rf-refiring svg { animation: none; }
    .rf-btn.rf-tick-a, .rf-btn.rf-tick-b { animation: none; }
    .rf-btn.rf-tick-a svg, .rf-btn.rf-tick-b svg { animation: none; }
    .rf-btn.rf-poll-a, .rf-btn.rf-poll-b { animation: none; }
  }

  /* ── Market-closed confirmation popup ────────────────────────────
     Local-positioned popup anchored to the RefreshButton via the
     .rf-wrap relative parent. Sits below the button + right-aligned
     so it can be reached from page-header callsites without
     overflowing the viewport on narrow viewports. */
  .rf-wrap {
    position: relative;
    display: inline-flex;
  }
  .rf-closed-overlay {
    position: fixed;
    inset: 0;
    background: transparent;
    z-index: 1000;
  }
  .rf-closed-popup {
    /* Composes .algo-modal chrome (gradient + amber halo + shadow +
       flex column + overflow hidden). Overrides:
       - positioning: absolute right-anchored below the RefreshButton
         (not centered viewport).
       - background: elevated navy — pill anchors to the toolbar chip
         and needs the +1 elevation step so it visually detaches from
         the navbar row.
       - border-radius: 5px (slightly tighter than canonical 6px)
         — matches the pill's own radius. */
    position: absolute;
    top: calc(100% + 0.4rem);
    right: 0;
    z-index: 1001;
    min-width: 16rem;
    max-width: min(18rem, 92vw);
    padding: 0.7rem 0.85rem 0.65rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border-radius: 5px;
    color: var(--text-primary);
    font-size: var(--fs-md);
    line-height: 1.4;
  }
  .rf-closed-title {
    font-weight: 700;
    font-size: var(--fs-lg);
    color: var(--c-action);
    margin-bottom: 0.35rem;
    letter-spacing: 0.03em;
  }
  .rf-closed-body {
    margin-bottom: 0.6rem;
    color: #94a3b8;
  }
  .rf-closed-actions {
    display: flex;
    gap: 0.45rem;
    justify-content: flex-end;
  }
  .rf-closed-btn {
    padding: 0.3rem 0.65rem;
    border-radius: 3px;
    font-size: var(--fs-sm);
    font-weight: 600;
    cursor: pointer;
    background: transparent;
    border: 1px solid transparent;
    color: #c8d8f0;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  /* Renamed from .rf-closed-cancel — this is the ONLY action in the
     popup (informational dismiss), not one half of a confirm/cancel pair. */
  .rf-closed-ok {
    border-color: rgba(126, 151, 184, 0.45);
    color: #94a3b8;
  }
  .rf-closed-ok:hover {
    background: rgba(126, 151, 184, 0.14);
    color: #c8d8f0;
  }
  /* (.rf-badge + .rf-badge-* CSS removed in audit-cleanup pass —
     the connection-state digit moved to the navbar broker-chip in
     slice AX. This button kept the full N/M tooltip; only the
     visible badge moved.) */
</style>
