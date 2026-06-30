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
  import { connStatus, startConnStatusPoller, lastRefreshAt, formatDualTz, visibleInterval } from '$lib/stores';
  import { isNseOpen, isMcxOpen } from '$lib/marketHours';
  import { symbolTickCount } from '$lib/data/symbolStore.svelte.js';
  import { onMount, onDestroy } from 'svelte';

  /**
   * @typedef {object} Props
   * @property {() => void} onClick - Click handler that should trigger the refresh.
   * @property {boolean} [loading] - Spinner state; click is suppressed while true.
   * @property {string} [label] - aria-label suffix (e.g. "positions", "audit").
   */
  /** @type {Props} */
  let { onClick, loading = false, label = 'data' } = $props();

  // Ensure the global connection-status poller is running. Idempotent —
  // safe to call from every mounted RefreshButton.
  onMount(() => { startConnStatusPoller(); });

  // Market-state tick — recomputes isNseOpen / isMcxOpen every 30 s so
  // the button's palette tracks session boundaries automatically. The
  // three buckets the operator cares about: both open (full hours),
  // only MCX open (commodity-only window), market closed.
  // Uses visibleInterval: pauses when hidden, fires immediately on return.
  let _nseOpen = $state(isNseOpen());
  let _mcxOpen = $state(isMcxOpen());
  /** @type {(() => void) | null} */
  let _mktTimer = null;
  onMount(() => {
    const tick = () => {
      _nseOpen = isNseOpen();
      _mcxOpen = isMcxOpen();
    };
    _mktTimer = visibleInterval(tick, 30_000);
  });
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
  onMount(() => {
    // Subscribe inside onMount (not $effect) so the subscription is
    // registered ONCE on mount; the callback firing per tick does not
    // re-run an $effect body and doesn't touch reactive state, so it
    // cannot cascade scheduler work.
    //
    // STUCK-SPINNER ROOT CAUSE (Jun 2026 reaudit): the per-tick rotation
    // animation (`rf-tick-rotate`, 0.25s finite) used to share the SAME
    // <svg> element with the loading-state animation (`rf-spin`, 0.9s
    // infinite). The `animation` CSS property is a shorthand — applying
    // it a second time RESETS the first. Cascade order put the tick-rotate
    // rule AFTER the rf-spin rule, so on every SSE tick during a refresh
    // the spinner would briefly do a 180° rotate-then-freeze cycle and
    // appear stuck. Fix here: SKIP the per-tick toggle entirely while
    // `loading` is true. The spinner itself is the busy signal — the
    // tick-pulse cosmetic is meaningless during a manual refresh. The CSS
    // also gains a defensive `:not(.rf-spinning)` guard on the tick-rotate
    // selector so even if the class somehow leaked through, the spin
    // animation would still win.
    _pulseUnsub = symbolTickCount.subscribe(() => {
      if (_pulseTimer) return;
      // Skip class toggle while spinner is active — see comment above.
      if (loading) return;
      _pulseTimer = setTimeout(() => {
        // Re-check at fire time — `loading` may have flipped true between
        // subscribe and timer fire.
        if (!loading) {
          _tickPulseClass = _tickPulseClass === 'rf-tick-a' ? 'rf-tick-b' : 'rf-tick-a';
        }
        _pulseTimer = null;
      }, 250);
    });
  });

  // Belt-and-suspenders: when `loading` flips true, immediately clear any
  // residual tick-pulse class so the in-flight tick animation cannot
  // override the spin animation via cascade. Tracks the prop transition
  // and only writes when the value actually changes (no chain re-fires).
  $effect(() => {
    if (loading && _tickPulseClass !== '') {
      _tickPulseClass = '';
    }
  });
  onDestroy(() => {
    _mktTimer?.();   // visibleInterval teardown (stops the interval + removes listener)
    if (_pulseTimer) clearTimeout(_pulseTimer);
    _pulseUnsub?.();
    _unsubLast?.();
    _unsubConn?.();
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
  const _mktTooltip = $derived(
    _nseOpen && _mcxOpen ? 'Equity + MCX open'
    : _mcxOpen            ? 'MCX open · Equity closed'
    :                       'Market closed'
  );
  // Closed-market notice popup. Operator: "when market is closed and
  // pressing refresh button should show popup stating market is
  // closed" — the slate palette during the market-closed state reads
  // as disabled, so the operator clicks and sees no feedback. Click
  // during market-closed now opens an informational popup explaining
  // why. The page-level pollers continue to auto-refresh in the
  // background ("it refreshes on its own for any data that is fine"
  // — operator), so manual refresh during a closed session is
  // intentionally a no-op.
  let _showClosedNotice = $state(false);
  function _handleClick() {
    if (loading) return;
    if (!_nseOpen && !_mcxOpen) {
      _showClosedNotice = true;
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
    queueMicrotask(() => { try { onClick?.(); } catch (e) { console.warn('[refresh] onClick threw:', e); } });
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

  onMount(() => {
    _unsubLast = lastRefreshAt.subscribe((v) => { _lastTs = v || 0; });
    _unsubConn = connStatus.subscribe((v) => {
      _loaded = Number(v?.loaded) || 0;
      _total  = Number(v?.total)  || 0;
      _backendOk = v?.backendOk !== false; // default true
      _failingAccounts = Array.isArray(v?.failingAccounts) ? v.failingAccounts : [];
    });
  });

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
    if (loading) {
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
  class="rf-btn {_mktClass} {_tickPulseClass}"
  class:rf-spinning={loading}
  onclick={(e) => { e.stopPropagation(); _handleClick(); }}
  disabled={loading}
  aria-label={loading ? `Refreshing ${label}` : `Refresh ${label}`}
  title={_connTitle}>
  {#if loading}
    <!-- Loading state — distinct arc-spinner glyph (NOT the same
         refresh-arrow rotated). Reads as "working / fetching" rather
         than "refresh affordance". The arc spins via the rf-spinning
         keyframe below. -->
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
  <div class="rf-closed-popup" role="dialog" aria-modal="true"
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
    background: rgba(251, 191, 36, 0.14);
    border-color: var(--algo-amber-border);
    color: #fbbf24;
  }
  .rf-mkt-mcx:hover:not(:disabled) {
    background: rgba(251, 191, 36, 0.22);
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
     The `:not(.rf-spinning)` guard ensures the rotate animation NEVER
     touches the spinner SVG while the button is in loading state — see
     rf-spin rule above for the full root-cause story. */
  .rf-btn.rf-tick-a, .rf-btn.rf-tick-b {
    animation: rf-tick-pulse 0.25s ease-out;
  }
  .rf-btn.rf-tick-a:not(.rf-spinning) svg,
  .rf-btn.rf-tick-b:not(.rf-spinning) svg {
    animation: rf-tick-rotate 0.25s ease-in-out;
  }
  @keyframes rf-tick-pulse {
    0%   { box-shadow: 0 0 0 0 rgba(125, 211, 252, 0.55); }
    100% { box-shadow: 0 0 8px 2px rgba(125, 211, 252, 0); }
  }
  @keyframes rf-tick-rotate {
    from { transform: rotate(0deg); }
    to   { transform: rotate(180deg); }
  }
  @media (prefers-reduced-motion: reduce) {
    .rf-btn.rf-spinning svg { animation: none; }
    .rf-btn.rf-tick-a, .rf-btn.rf-tick-b { animation: none; }
    .rf-btn.rf-tick-a svg, .rf-btn.rf-tick-b svg { animation: none; }
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
    position: absolute;
    top: calc(100% + 0.4rem);
    right: 0;
    z-index: 1001;
    min-width: 16rem;
    max-width: min(18rem, 92vw);
    padding: 0.7rem 0.85rem 0.65rem;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(251, 191, 36, 0.55);
    border-radius: 5px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
    color: #c8d8f0;
    font-size: 0.65rem;
    line-height: 1.4;
  }
  .rf-closed-title {
    font-weight: 700;
    font-size: 0.7rem;
    color: #fbbf24;
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
    font-size: 0.62rem;
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
