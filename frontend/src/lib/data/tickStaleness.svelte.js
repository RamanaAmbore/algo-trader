/**
 * tickStaleness.svelte.js — client-observed staleness watchdog for the
 * live LTP SSE tick stream (quoteStream.js).
 *
 * Background (2026-10): a backend bug caused the KiteTicker→SSE tick
 * bridge to go silently dead for hours while the SSE CONNECTION itself
 * stayed healthy (heartbeats kept flowing) — the only operator-visible
 * symptom was "LTP looks frozen" with zero on-screen cue. This module is
 * the frontend-only fix: it watches `lastLiveTickAt` (quoteStream.js),
 * which only advances on a real 'snapshot'/'tick' SSE event carrying at
 * least one valid LTP — never on a bare heartbeat, and never on a
 * background book-poller batch (positions/holdings/funds pollers write via
 * a completely separate path and never touch `lastLiveTickAt`). This is a
 * deliberately different signal from `symbolTickCount`
 * (symbolStore.svelte.js), which bumps on poller batches too and would
 * never go quiet during this exact incident.
 *
 * Singleton — one checker for the whole app regardless of how many
 * RefreshButton instances are mounted on a page (some pages mount 2-3).
 * `startTickStalenessWatch()` is idempotent, called from every mounted
 * RefreshButton (same convention as `startConnStatusPoller()`), so exactly
 * one checker runs and exactly one toast fires per staleness episode.
 *
 * Market-open gating reuses the SAME `isNseOpen()`/`isMcxOpen()` functions
 * RefreshButton.svelte's own palette logic already calls (not reinvented),
 * per the design brief — ticks are only "expected" while a segment is open,
 * so the indicator/toast never fires on a quiet-but-correct closed market.
 *
 * Purely client-side: no new backend endpoint, no new API call. Reset
 * semantics: the "toast already fired" flag clears ONLY when a genuine new
 * tick lands (not when the market merely closes) — closing the market
 * hides the badge (AND-gated) but a still-dead stream that reappears after
 * reopen must not re-flood with a second toast for the same episode.
 */

import { visibleInterval } from '../stores.js';
import { isNseOpen, isMcxOpen } from '../marketHours.js';
import { lastLiveTickAt } from './quoteStream.js';
import { toast } from './toastStore.svelte.js';

/**
 * Threshold (ms) of SSE tick silence, while the market is open, before the
 * stream is considered stale. Overridable for Playwright via
 * `window.__rbq_tickStaleMs` (set with `addInitScript` before navigation —
 * same precedent as stores.js's `window.__rbq_hibMs`), so a spec can avoid
 * waiting 60-90 real seconds.
 */
const TICK_STALE_MS = (() => {
  if (typeof window !== 'undefined' && typeof (/** @type {any} */ (window).__rbq_tickStaleMs) === 'number') {
    return /** @type {any} */ (window).__rbq_tickStaleMs;
  }
  return 75_000; // 75s — midpoint of the operator-specified 60-90s window
})();

// ── Reactive backing state (module-level $state singleton, mirrors the
//    activityStore.svelte.js / toastStore.svelte.js convention) ──────────
let _ticksStale = $state(false);
let _ticksStaleSinceLabel = $state('');

/** Public read-only surface consumed by RefreshButton.svelte. */
export const tickStaleness = {
  /** True while the market is open AND no real SSE tick/snapshot has
   *  landed for TICK_STALE_MS. Drives the amber "TICKS STALE" badge. */
  get stale() { return _ticksStale; },
  /** IST HH:MM of the last real tick/snapshot, captured once at the
   *  moment staleness flips true (not re-rendered every check). */
  get sinceLabel() { return _ticksStaleSinceLabel; },
};

let _started = false;
// Plain (non-reactive) — read only by the 5s checker below, never rendered
// directly. Mirrors RefreshButton's own _pulseTimer-style plain vars; a
// $state here would re-run every subscriber effect on every tick for no
// visual benefit.
let _lastTickAt = Date.now();
// Wall-clock ms of the last time the tab transitioned to visible. Grace-
// window anchor for the Safari-background-tab false-positive fix (2026-10):
// stores.js's `exitHibernation()` runs every registered `visibleInterval`
// callback SYNCHRONOUSLY and IMMEDIATELY on tab-return — including this
// module's own `_check()` — before quoteStream.js's own visibilitychange
// handler has had a chance to reconnect and deliver a fresh tick. Safari
// suspends background tabs more aggressively than Chrome, so `_lastTickAt`
// can be genuinely stale at that exact instant even though the stream
// recovers correctly a moment later. See `_onVisibilityChange` below.
let _lastVisibleAt = Date.now();
let _toastFired = false;
/** @type {(() => void) | null} */
let _unsubTick = null;
/** @type {(() => void) | null} */
let _checkTeardown = null;

function _fmtIstHHMM(/** @type {number} */ ts) {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata',
    }).format(ts) + ' IST';
  } catch (_) {
    return '';
  }
}

function _check() {
  // Never evaluate while the tab is hidden — a check landing during the
  // pre-hibernation idle window, or a throttled background timer catching
  // up, must never flip the stale flag based on a tab the operator isn't
  // looking at. Without this, the badge/toast could already be showing
  // the instant the tab becomes visible again (same false-positive class
  // the grace window below fixes for the moment right AFTER return).
  if (document.hidden) return;
  const marketOpen = isNseOpen() || isMcxOpen();
  if (!marketOpen) {
    // Hide while closed (AND-gated display) but leave _toastFired alone —
    // a still-dead stream reappearing after reopen is the SAME episode,
    // not a new one; only a genuine tick (see the subscriber below) may
    // re-arm the toast.
    if (_ticksStale) _ticksStale = false;
    return;
  }
  // Grace window: suppress a stale verdict until TICK_STALE_MS has elapsed
  // since EITHER the last real tick OR the last time the tab became
  // visible again, whichever is more recent (see `_lastVisibleAt` comment
  // above for why this exists).
  const gap = Date.now() - Math.max(_lastTickAt, _lastVisibleAt);
  if (gap < TICK_STALE_MS) return;
  if (!_ticksStale) {
    _ticksStaleSinceLabel = _fmtIstHHMM(_lastTickAt);
    _ticksStale = true;
  }
  if (!_toastFired) {
    _toastFired = true;
    try {
      toast.warning('Live ticks stale — no updates 75s+', { timeoutMs: 8000 });
    } catch (_) { /* toast store unavailable — silent */ }
  }
}

/**
 * Records the moment the tab becomes visible again. Registered on
 * `window` with `capture: true` (not `document`, not bubble-phase) so it
 * runs during the CAPTURING phase — which always fires before any
 * target-phase listener on `document` regardless of registration order.
 * This matters because `stores.js`'s own global `visibilitychange`
 * listener (installed on `document`, the first time ANY `visibleInterval`
 * is created — almost certainly before this module's own, since many
 * layout-level pollers mount first) calls `exitHibernation()`
 * synchronously and immediately, which in turn runs THIS module's
 * `_check()` (via its own `visibleInterval`) in the very same event
 * dispatch. Without the capture-on-window trick, `_lastVisibleAt` would
 * update too late — `_check()` would already have read the stale value.
 * @param {Event} _e
 */
function _onVisibilityChange(_e) {
  if (document.visibilityState === 'visible') {
    _lastVisibleAt = Date.now();
  }
}

/** @type {boolean} */
let _visHandlerInstalled = false;

/**
 * Start the staleness watchdog. Idempotent — safe to call from every
 * mounted RefreshButton; only the first call does any work.
 */
export function startTickStalenessWatch() {
  if (_started) return;
  _started = true;
  _unsubTick = lastLiveTickAt.subscribe((ts) => {
    if (!ts) return; // initial 0 value — no real tick yet
    _lastTickAt = ts;
    if (_ticksStale) _ticksStale = false;
    _toastFired = false;
  });
  if (!_visHandlerInstalled) {
    _visHandlerInstalled = true;
    window.addEventListener('visibilitychange', _onVisibilityChange, true);
  }
  _checkTeardown = visibleInterval(_check, 5_000);
}

/** Test-only teardown (not called by any production path — RefreshButton
 *  instances intentionally never stop this singleton on unmount, mirroring
 *  startConnStatusPoller's lifetime). Exposed so a future test harness can
 *  reset module state between runs if ever needed. */
export function _stopTickStalenessWatchForTests() {
  _unsubTick?.();
  _checkTeardown?.();
  if (_visHandlerInstalled) {
    window.removeEventListener('visibilitychange', _onVisibilityChange, true);
    _visHandlerInstalled = false;
  }
  _unsubTick = null;
  _checkTeardown = null;
  _started = false;
  _ticksStale = false;
  _ticksStaleSinceLabel = '';
  _toastFired = false;
  _lastTickAt = Date.now();
  _lastVisibleAt = Date.now();
}
