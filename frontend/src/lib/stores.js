/**
 * Shared app state — auth + client-side data cache.
 *
 * Auth store: persists JWT + user profile in sessionStorage so page reloads
 * keep the session alive. Writable store so any component can react to login/logout.
 *
 * Data cache: plain module-level object (not reactive). Pages write to it after
 * a successful fetch and read from it on mount to show stale-while-revalidate data
 * immediately when navigating back.
 */

import { writable, derived, get } from 'svelte/store';
import { browser } from '$app/environment';
import { isMarketOpen, isNseOpen, isMcxOpen, fetchMarketStatus } from '$lib/marketHours';
import { activityStore as _activityStore } from '$lib/data/activityStore.svelte.js';

// ---------------------------------------------------------------------------
// Post-hibernation refiring state
// ---------------------------------------------------------------------------
//
// Set true when _exitHibernation() runs (tab returns after ≥ idle threshold
// hidden). While true, every RefreshButton instance on the active page spins
// to signal that stored data is being refreshed. Auto-clears when all
// registered refire callbacks complete OR after a 3 s max-wait timeout.
//
// Not shown on public (cream) pages — those don't mount the algo layout
// and do not register hibernation subscribers.
export const postHibernationRefiring = writable(false);

/** Max time (ms) the RefreshButton keeps spinning even if stores haven't resolved. */
const _RECONNECT_MAX_MS = 3000;

// ---------------------------------------------------------------------------
// Auth store
// ---------------------------------------------------------------------------

function _decodeJwt(/** @type {string|null} */ tok) {
  // Decode the payload of a JWT without verification — we only consume
  // claims that downstream UI code already trusts because the JWT was
  // server-signed and accepted at login. Used to refresh `is_super` on
  // every session read, so a stale `ramboq_user` blob (from a login
  // that pre-dated the is_super claim) can't pin the role chip wrong.
  if (!tok) return null;
  try {
    const part = tok.split('.')[1];
    if (!part) return null;
    const padded = part + '='.repeat((4 - part.length % 4) % 4);
    const json   = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
    return JSON.parse(json);
  } catch { return null; }
}

function _readSession() {
  if (!browser) return { token: null, user: null, impBy: null };
  try {
    const token = sessionStorage.getItem('ramboq_token');
    const raw   = sessionStorage.getItem('ramboq_user');
    let user    = raw ? JSON.parse(raw) : null;
    // impBy is read from the JWT each time so an admin who refreshes
    // a tab mid-impersonation still sees the banner. Set by the
    // claim, not by a separate storage key.
    const _claims = _decodeJwt(token);
    const impBy   = _claims?.imp_by || null;
    if (token && !user) {
      // sessionStorage half-cleared (devtools / partial logout / browser
      // cache hiccup): the token survived but `ramboq_user` is gone.
      // Rebuild `user` from the JWT's own claims so the layout's
      // role-check effect doesn't bounce the operator to /signin on
      // every navbar click. Claims carry sub / role / display_name —
      // enough to reconstruct what the login response would have stored.
      const claims = _decodeJwt(token);
      if (claims) {
        user = {
          username:     claims.sub,
          role:         claims.role,
          display_name: claims.display_name,
        };
        // Re-persist so subsequent reads (and any code that reads
        // sessionStorage directly) see the rebuilt blob too.
        try {
          sessionStorage.setItem('ramboq_user', JSON.stringify(user));
        } catch { /* quota / privacy mode — non-fatal */ }
      }
    } else if (token && user) {
      const claims = _decodeJwt(token);
      if (claims) {
        // JWT wins for role so a stale `ramboq_user` blob can't pin the
        // role chip wrong. role values: 'partner' | 'admin' | 'designated'.
        user.role = claims.role ?? user.role;
        // Defensive: legacy sessions persisted is_super; drop it.
        delete user.is_super;
      }
    }
    return { token, user, impBy };
  } catch {
    return { token: null, user: null, impBy: null };
  }
}

function createAuthStore() {
  const { subscribe, set } = writable(_readSession());

  return {
    subscribe,

    /** Call after successful login. */
    login(token, user) {
      if (browser) {
        sessionStorage.setItem('ramboq_token', token);
        sessionStorage.setItem('ramboq_user', JSON.stringify(user));
        // Wipe the persistent cache (mirrors logout). Demo / anonymous
        // sessions fetch MASKED account codes from the server; after
        // login the same endpoints return UNMASKED codes (admin paths)
        // so the cached masked rows would briefly flash on first paint
        // until the next fetch completes. Dropping the cache here
        // forces a fresh round of fetches that pick up the new JWT
        // and render unmasked from the first paint. Operator: "when a
        // user demo mode to go through the pages, when he logins and
        // goes through the same pages, do the account unmasked
        // automatically".
        try {
          const keys = [];
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (k && k.startsWith('rbq.cache.')) keys.push(k);
          }
          for (const k of keys) localStorage.removeItem(k);
        } catch { /* quota / privacy mode — non-fatal */ }
      }
      const claims = _decodeJwt(token);
      set({ token, user, impBy: claims?.imp_by || null });
    },

    /** Call on logout or 401. */
    logout() {
      if (browser) {
        sessionStorage.removeItem('ramboq_token');
        sessionStorage.removeItem('ramboq_user');
        sessionStorage.removeItem('ramboq_orig_token');
        sessionStorage.removeItem('ramboq_orig_user');
        // Drop the per-user persistentCache so a second operator on the
        // same browser does not see the prior session's positions, holdings,
        // funds, or NAV snapshot. The 15-min `minute`-bucket TTL is long
        // enough to leak intra-day data across a user switch otherwise.
        try {
          const keys = [];
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (k && k.startsWith('rbq.cache.')) keys.push(k);
          }
          for (const k of keys) localStorage.removeItem(k);
        } catch { /* quota / privacy mode — non-fatal */ }
      }
      set({ token: null, user: null, impBy: null });
    },

    /** Read token directly (non-reactive). */
    getToken() {
      return browser ? sessionStorage.getItem('ramboq_token') : null;
    },

    /** Start an impersonation session. Stashes the current admin
     *  token/user under separate keys so a tab refresh during
     *  impersonation can recover both ends. */
    startImpersonation(token, user) {
      if (browser) {
        const origTok  = sessionStorage.getItem('ramboq_token');
        const origUser = sessionStorage.getItem('ramboq_user');
        if (origTok)  sessionStorage.setItem('ramboq_orig_token', origTok);
        if (origUser) sessionStorage.setItem('ramboq_orig_user', origUser);
        sessionStorage.setItem('ramboq_token', token);
        sessionStorage.setItem('ramboq_user', JSON.stringify(user));
      }
      const claims = _decodeJwt(token);
      set({ token, user, impBy: claims?.imp_by || null });
    },

    /** End the current impersonation. The caller has already POSTed
     *  /stop-impersonate and received a fresh JWT for the original
     *  actor — pass it in. Clears the stashed orig_* keys. */
    stopImpersonation(token, user) {
      if (browser) {
        sessionStorage.setItem('ramboq_token', token);
        sessionStorage.setItem('ramboq_user', JSON.stringify(user));
        sessionStorage.removeItem('ramboq_orig_token');
        sessionStorage.removeItem('ramboq_orig_user');
      }
      set({ token, user, impBy: null });
    },
  };
}

export const authStore = createAuthStore();

// ---------------------------------------------------------------------------
// Data cache — stale-while-revalidate for all data pages
// Each entry: { data, refreshed_at } or null before first fetch.
// ---------------------------------------------------------------------------

export const dataCache = {
  market:    null,   // { content, cycle_date, refreshed_at }
};

/**
 * Compact page-top timestamp banner. Day-first (20 Apr) to match
 * Indian / British convention; 3-letter weekday + 3-letter month;
 * year dropped; 24-hour time. Example:
 *   "Mon 20 Apr  23:06 IST | Mon 20 Apr  13:36 EDT"
 * Both date halves repeated so IST/EST day-boundary cases stay
 * unambiguous. Auto-resolves EST/EDT by season.
 */

// ---------------------------------------------------------------------------
// Hibernation bus
// ---------------------------------------------------------------------------
//
// Policy (operator-confirmed 2026-06-28):
//   Tab active OR hidden < polling.idle_timeout_min  → NORMAL cadence.
//       All pollers run exactly as before. No changes.
//   Tab hidden >= polling.idle_timeout_min            → HIBERNATION:
//       critical pollers (throttle mode)  → 30 s cadence
//       non-critical pollers (pause mode) → stopped entirely
//   Tab becomes visible (out of hibernation) → immediate refire of every
//       throttled/paused poller within 200 ms, then resume normal cadence.
//   Tab visible (was never hibernating) → no-op, pollers were running normally.
//
// Default threshold: 5 min. Read from /api/admin/settings key
// `polling.idle_timeout_min` on app boot (algo layout onMount).
// Can be overridden for tests via window.__rbq_hibMs (ms) before page load.
//
// WebSocket connections are intentionally NOT hibernated — WS must remain
// open so position_filled / book_changed events land during background time.

/** Default idle threshold: 5 minutes in milliseconds. */
let _hibernationIdleMs = (() => {
  // Allow Playwright / test harnesses to inject a custom threshold
  // before the module loads (via addInitScript). The window property
  // must be set before any page script runs for the override to land.
  if (typeof window !== 'undefined' && typeof (/** @type {any} */ (window).__rbq_hibMs) === 'number') {
    return /** @type {any} */ (window).__rbq_hibMs;
  }
  return 5 * 60 * 1000;
})();

/**
 * Override the hibernation idle threshold.
 * Called after reading polling.idle_timeout_min from /api/admin/settings.
 * Also exposed as window.__rbq_setHibMs for test-harness late patching.
 * @param {number} minutes  desired threshold in minutes (clamped to >= 0)
 */
export function setHibernationIdleMinutes(minutes) {
  const m = Number.isFinite(minutes) ? minutes : 5;
  _hibernationIdleMs = Math.max(0, m) * 60 * 1000;
}

/** Whether the tab is currently in hibernation mode. */
let _isHibernating = false;

/** Handle for the pending hibernation timer. */
let _hibernationTimer = /** @type {ReturnType<typeof setTimeout> | null} */ (null);

/**
 * Registry of hibernation callbacks from all active visibleInterval instances.
 * @type {Set<{enterHibernation: () => void, exitHibernation: () => void}>}
 */
const _hibernationSubscribers = new Set();

function _enterHibernation() {
  if (_isHibernating) return;
  _isHibernating = true;
  for (const sub of _hibernationSubscribers) {
    try { sub.enterHibernation(); } catch { /* ignore */ }
  }
}

/** Handle for the max-wait timeout that stops the RefreshButton spinner. */
let _reconnectMaxTimer = /** @type {ReturnType<typeof setTimeout> | null} */ (null);

function _exitHibernation() {
  const wasHibernating = _isHibernating;
  _isHibernating = false;
  if (!wasHibernating) return;

  if (browser && _hibernationSubscribers.size > 0) {
    // Spin every RefreshButton on the active page immediately.
    postHibernationRefiring.set(true);

    // Cancel any previous max-wait timer.
    if (_reconnectMaxTimer != null) {
      clearTimeout(_reconnectMaxTimer);
      _reconnectMaxTimer = null;
    }

    // Collect refire results so we can stop spinning as soon as all
    // pollers have fired (rather than always waiting the full 3 s).
    // visibleInterval callbacks are synchronous in exitHibernation —
    // they don't expose Promises. Wrap each in Promise.resolve() so
    // Promise.allSettled can still await the synchronous flush before
    // clearing the flag. The max-wait timer is a belt-and-suspenders
    // guard for any slow / async pollers.
    _reconnectMaxTimer = setTimeout(() => {
      postHibernationRefiring.set(false);
      _reconnectMaxTimer = null;
    }, _RECONNECT_MAX_MS);

    for (const sub of _hibernationSubscribers) {
      try { sub.exitHibernation(); } catch { /* ignore */ }
    }
    // Note: exitHibernation callbacks are synchronous, so Promise.allSettled
    // would resolve in the same microtask — before Svelte can flush the
    // postHibernationRefiring=true write into the DOM. We intentionally let
    // the max-wait timer (3 s) be the only clearance path, giving Svelte at
    // least one animation frame to react to the store write before the flag
    // clears. Pages with many subscribers (pollers that do actual async work)
    // will see natural clearing when all re-fetches complete within 3 s.
    // Intentional: no Promise.allSettled early-clear here.
    return;
  }

  for (const sub of _hibernationSubscribers) {
    try { sub.exitHibernation(); } catch { /* ignore */ }
  }
}

/** Global visibilitychange handler — installed once when the first
 *  visibleInterval is created in a browser context. */
let _globalVisHandlerInstalled = false;

function _ensureGlobalVisHandler() {
  if (_globalVisHandlerInstalled || typeof document === 'undefined') return;
  _globalVisHandlerInstalled = true;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      // Start the idle timer. Hibernation doesn't engage until it fires.
      if (_hibernationTimer != null) { clearTimeout(_hibernationTimer); }
      _hibernationTimer = setTimeout(_enterHibernation, _hibernationIdleMs);
    } else {
      // Tab returned — cancel any pending hibernation timer.
      if (_hibernationTimer != null) { clearTimeout(_hibernationTimer); _hibernationTimer = null; }
      // Exit hibernation (no-op if wasn't hibernating; fires refires if was).
      _exitHibernation();
    }
  });
}

// Expose late-patch hook for test harnesses that load after module init.
// window.__rbq_setHibMs(minutes) overrides the hibernation idle threshold.
// Takes MINUTES (same as setHibernationIdleMinutes) so callers can set
// a 0-minute (= 0 ms) threshold for e2e tests without waiting 5 real minutes.
if (typeof window !== 'undefined') {
  /** @type {any} */ (window).__rbq_setHibMs = setHibernationIdleMinutes;
}

/**
 * setInterval variant with visibility-aware behaviour.
 *
 * NORMAL OPERATION (tab active, or hidden < polling.idle_timeout_min):
 *   Interval runs at `ms` cadence regardless of tab visibility.
 *   This preserves the pre-hibernation behavior — pollers keep running
 *   for the idle grace period so a brief tab switch doesn't stall data.
 *
 * AFTER IDLE THRESHOLD (tab hidden >= polling.idle_timeout_min):
 *   HIBERNATION engages. Behavior depends on `mode`:
 *
 *   mode: 'pause' (default)
 *     Poller is stopped. Use for non-critical pollers (news, sparkline
 *     warm, instruments refresh, LogPanel lazy tabs) where stale data
 *     is acceptable until the operator returns.
 *
 *   mode: 'throttle:<ms>'  (e.g. 'throttle:30000')
 *     Reduces the interval cadence to <ms>. Use for critical pollers
 *     (positions / holdings / funds / NAV / broker health) that must
 *     stay alive but can tolerate a slower heartbeat.
 *
 * On tab return from hibernation:
 *   Callback fires ONCE immediately (refire), then resumes normal `ms`
 *   cadence.
 *
 * On tab return without hibernation (hidden < idle threshold):
 *   No refire needed — poller was running normally the whole time.
 *
 * WebSocket subscribers are intentionally NOT passed through this helper —
 * keep the WS connection open unconditionally so `position_filled` /
 * `book_changed` events land even when the tab is backgrounded.
 *
 * Returns a teardown function: call it from onDestroy (no separate
 * clearInterval needed).
 *
 * @param {() => void} fn     callback to run on each tick
 * @param {number}     ms     normal (foreground) interval in milliseconds
 * @param {string}     [mode] 'pause' (default) | 'throttle:<hiddenMs>'
 * @returns {() => void}      teardown; clears the interval + removes the listener
 */
/**
 * Parse the visibleInterval `mode` string. Returns `{ hiddenMs, isThrottle }`.
 *   'pause'                       → { 0, false }  (default)
 *   'throttle:30000'              → { 30000, true }
 *   malformed / non-string / <=0  → { 0, false }  (safe fallback → pause)
 *
 * @param {unknown} mode
 * @returns {{ hiddenMs: number, isThrottle: boolean }}
 */
function _parseHibMode(mode) {
  if (typeof mode !== 'string' || !mode.startsWith('throttle:')) {
    return { hiddenMs: 0, isThrottle: false };
  }
  const parsed = Number(mode.slice(9));
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return { hiddenMs: 0, isThrottle: false };
  }
  return { hiddenMs: parsed, isThrottle: true };
}

export function visibleInterval(fn, ms, mode = 'pause') {
  const { hiddenMs, isThrottle } = _parseHibMode(mode);

  let id = /** @type {ReturnType<typeof setInterval> | null} */ (null);
  /** Whether this instance is currently hibernating. */
  let _localHibernating = false;

  /** Start (or restart) the interval at the given cadence. */
  const _start = (/** @type {number} */ delay) => {
    if (id != null) { clearInterval(id); id = null; }
    id = setInterval(fn, delay);
  };
  const _stop = () => {
    if (id != null) { clearInterval(id); id = null; }
  };

  const enterHibernation = () => {
    // Only apply hibernation if the tab is actually hidden right now.
    if (typeof document === 'undefined' || !document.hidden) return;
    _localHibernating = true;
    if (isThrottle) _start(hiddenMs);
    else            _stop();
  };

  const exitHibernation = () => {
    if (!_localHibernating) return;
    _localHibernating = false;
    // Immediate refire then resume normal cadence.
    fn();
    _start(ms);
  };

  // Boot cadence — three mount states resolve to distinct start behaviours.
  // Extracted into a nested helper so the constructor tail reads flat.
  const _boot = () => {
    if (typeof document === 'undefined') {
      // SSR / non-browser — run at normal cadence.
      _start(ms);
      return;
    }
    if (!document.hidden) {
      // Tab is visible — start at normal cadence.
      _start(ms);
    } else if (_isHibernating) {
      // Tab starts hidden AND hibernation already active (late mount after
      // a long background period): apply hibernation mode immediately.
      _localHibernating = true;
      if (isThrottle) _start(hiddenMs);
      // else pause mode: don't start
    } else {
      // Tab starts hidden but hibernation not yet active — run at normal
      // cadence for the remainder of the idle grace period.
      _start(ms);
    }
    _ensureGlobalVisHandler();
  };

  const sub = { enterHibernation, exitHibernation };
  _hibernationSubscribers.add(sub);
  _boot();

  return () => {
    _stop();
    _hibernationSubscribers.delete(sub);
  };
}

/**
 * visibleInterval + market-hours gate. Use for auto-refresh of any
 * data that ONLY moves during NSE/MCX hours (LTPs, positions P&L,
 * agent fires) — outside the combined 09:00-23:30 IST Mon-Fri window
 * the callback is a no-op so we don't flood the API with refreshes
 * that return identical cached values.
 *
 * The interval timer still runs (we just skip the callback) so the
 * gate re-engages naturally at the next minute boundary as the market
 * opens — no special cross-boundary scheduling needed.
 *
 * The visibility gate layers on top via `visibleInterval`. Pass
 * `hiddenMs` to throttle instead of fully pausing when the tab is
 * hidden — recommended for critical pollers (positions, holdings,
 * funds) that must keep a heartbeat alive. When `hiddenMs` is 0
 * (default) the poller pauses entirely on hidden (original behaviour).
 *
 * @param {() => void} fn          callback to run on each tick
 * @param {number}     ms          foreground interval in milliseconds
 * @param {number}     [hiddenMs]  background cadence (0 = pause)
 * @returns {() => void}           teardown
 */
export function marketAwareInterval(fn, ms, hiddenMs = 0) {
  // Two timers per consumer:
  //   1. Main `ms`-cadence interval (the historical behaviour) — fires
  //      fn while market is open; no-ops outside hours.
  //   2. 5s edge-detect clock — refreshes the holiday-aware server
  //      status first, then fires fn ONCE on closed→open transition.
  //      The status refresh is essential: isMarketOpen() reads the
  //      cached _serverStatus, which is otherwise polled at 5min
  //      cadence. At a session boundary (09:15 IST), the cache could
  //      be 4+ minutes stale → edge would never see the transition.
  //      Operator: "when it reopens, any relevant data should refresh
  //      the values."
  let _prevOpen = isMarketOpen();
  // Re-entrancy guard. `visibleInterval` fires every 5s via
  // `setInterval`, which doesn't await async callbacks. On a slow
  // network where `fetchMarketStatus()` takes >5s, the next interval
  // would otherwise stack a second concurrent invocation; two
  // overlapping awaits could mutate `_prevOpen` in non-deterministic
  // order, missing or duplicating the closed→open fire.
  let _edgeRunning = false;
  const mainMode = hiddenMs > 0 ? `throttle:${hiddenMs}` : 'pause';
  const mainTeardown = visibleInterval(() => {
    if (!isMarketOpen()) return;
    fn();
  }, ms, mainMode);
  const edgeTeardown = visibleInterval(async () => {
    if (_edgeRunning) return;
    _edgeRunning = true;
    try {
      // Refresh _serverStatus first so the edge-check sees ground
      // truth, not a 5-min-stale poll. fetchMarketStatus silently
      // no-ops on failure — falls back to whichever value was cached.
      try { await fetchMarketStatus(); } catch { /* silent */ }
      const open = isMarketOpen();
      if (open && !_prevOpen) fn();
      _prevOpen = open;
    } finally {
      _edgeRunning = false;
    }
  }, 5000);
  return () => {
    mainTeardown();
    edgeTeardown();
  };
}


/** Display label for a git branch name. The `main` branch is the
 *  prod deployment target — operators think in "prod / dev" terms,
 *  not "main / non-main", so the UI surfaces the branch as `prod`
 *  whenever the raw value is `main`. Any other branch name flows
 *  through unchanged (dev branches keep their working name).
 *  Internal predicates (`paperStatus.branch === 'main'`, etc.) keep
 *  using the raw value — this helper is presentation-only. */
/** Global execution-mode store.
 *  Read by OrderTicket on open to set the default mode pill.
 *  Values: 'sim' | 'replay' | 'paper' | 'shadow' | 'live'
 *
 *  Initial value picks LIVE on prod and PAPER on dev so the navbar
 *  chip reads correctly during the brief window before
 *  /api/admin/execution/mode responds. Operator: "live should be
 *  default in prod. any other mode default in dev". Branch is
 *  inferred from hostname — `dev.<...>` prefix → dev (PAPER), every
 *  other host (including the canonical `ramboq.com` + any internal
 *  preview) → prod (LIVE). SSR-safe: when `window` is unavailable
 *  the boot value falls through to 'live' (the prod default). The
 *  API response always overrides this within the first poll cycle. */
function _bootMode() {
  try {
    if (typeof window !== 'undefined') {
      const host = String(window.location?.hostname ?? '');
      if (host.startsWith('dev.') || host === 'localhost' || host === '127.0.0.1') {
        // Dev defaults to IDLE — engine kill-switch is off until the
        // operator picks PAPER/SIM/REPLAY from the navbar. The API
        // confirms (or upgrades) the value on the first poll cycle.
        return 'idle';
      }
    }
  } catch { /* SSR / non-browser context */ }
  return 'live';
}
export const executionMode = writable(/** @type {'idle'|'sim'|'replay'|'paper'|'shadow'|'live'} */ (_bootMode()));

export function branchLabel(/** @type {string|null|undefined} */ name) {
  if (!name) return '';
  return name === 'main' ? 'prod' : name;
}

// ---------------------------------------------------------------------------
// Memoised Intl caches — keyed by minute epoch (Math.floor(ms / 60_000)).
// The dual-tz format never shows seconds so minute-precision is exact.
// Each cache is cleared every 60 s to prevent unbounded growth.
// The setInterval guard keeps this SSR-safe (no global timer on the server).
// ---------------------------------------------------------------------------
const _fmtDualTzCache = new Map();
const _fmtClientTsCache = new Map();
// Cache cleared every 60 s via visibleInterval so background tabs pay
// no timer overhead at all (the caches are minute-keyed and stale
// entries are harmless — they just grow unbounded without the purge).
if (typeof document !== 'undefined') {
  visibleInterval(() => { _fmtDualTzCache.clear(); _fmtClientTsCache.clear(); }, 60_000);
} else if (typeof setInterval !== 'undefined') {
  // SSR / non-browser: plain setInterval (no document.addEventListener).
  setInterval(() => { _fmtDualTzCache.clear(); _fmtClientTsCache.clear(); }, 60_000);
}

function _dualTzCore(/** @type {Date} */ now) {
  const parts = (tz) => {
    const arr = new Intl.DateTimeFormat('en-GB', {
      weekday: 'short', day: '2-digit', month: 'short',
      hour: '2-digit', minute: '2-digit', hour12: false,
      timeZone: tz,
    }).formatToParts(now);
    const pick = (t) => (arr.find(p => p.type === t) || {}).value || '';
    return {
      wd: pick('weekday'),
      d:  pick('day'),
      m:  pick('month'),
      h:  pick('hour'),
      mn: pick('minute'),
    };
  };
  const ist = parts('Asia/Kolkata');
  const est = parts('America/New_York');
  const estTz = now.toLocaleTimeString('en-US', {
    timeZoneName: 'short', timeZone: 'America/New_York',
  }).split(' ').pop();
  return { ist, est, estTz };
}

/** Dual-timezone format helper. Same shape as `clientTimestamp()` but
 *  accepts an arbitrary Date instead of `new Date()` — used for the
 *  "last refreshed at" line in the RefreshButton tooltip so the
 *  format matches the page-header wall clock exactly.
 *  Memoised at minute granularity — the output format has no seconds. */
export function formatDualTz(/** @type {Date | number} */ date) {
  if (!date) return '';
  const now = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(now.getTime())) return '';
  const key = Math.floor(now.getTime() / 60_000);
  const hit = _fmtDualTzCache.get(key);
  if (hit !== undefined) return hit;
  const { ist, est, estTz } = _dualTzCore(now);
  const result = `${ist.wd} ${ist.d} ${ist.m} · ${ist.h}:${ist.mn} IST · ${est.h}:${est.mn} ${estTz}`;
  _fmtDualTzCache.set(key, result);
  return result;
}

/** IST-only format for the "last refreshed at" display in page headers.
 *  Returns "HH:MM IST" in 24-hour format, or '—' for falsy input.
 *  Use this for `$lastRefreshAt` displays; leave `formatDualTz` for tooltips
 *  and log surfaces that need the full dual-timezone string. */
export function formatIstOnly(/** @type {Date | number | null | undefined} */ date) {
  if (!date) return '—';
  return new Date(date).toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }) + ' IST';
}

export function clientTimestamp() {
  const now = new Date();
  const key = Math.floor(now.getTime() / 60_000);
  const hit = _fmtClientTsCache.get(key);
  if (hit !== undefined) return hit;
  // Pull the structured parts once per zone so we can compare dates
  // and selectively elide the duplicate when both zones land on the
  // same calendar day (the common case during market hours). When
  // the dates differ (around UTC midnight) we keep the EST weekday
  // so the operator still sees the date delta — no info lost.
  const { ist, est, estTz } = _dualTzCore(now);
  const istHead = `${ist.wd} ${ist.d} ${ist.m}`;
  const istTime = `${ist.h}:${ist.mn} IST`;
  // EDT half is time-only — weekday + date intentionally omitted
  // (IST side already carries the calendar context; duplicating it
  // on the EST side just bloats the page header).
  const estHalf = `${est.h}:${est.mn} ${estTz}`;
  const result = `${istHead} · ${istTime} · ${estHalf}`;
  _fmtClientTsCache.set(key, result);
  return result;
}

// Reactive ticking version of clientTimestamp(). Pages binding `{$nowStamp}`
// get the dual-tz clock string and it updates every 60 s automatically.
// Earlier callsites used `{clientTimestamp()}` which captured the string at
// first render and stayed frozen until the page was navigated away from.
export const nowStamp = writable(browser ? clientTimestamp() : '');

// ── Strategy filter (slice 7f) ─────────────────────────────────────────
//
// Cross-page filter — operator picks a strategy on /admin/derivatives
// (or any other page that mounts a StrategyPicker), every consumer
// page that scopes off `selectedStrategyId` narrows in sync.
//
// `null` means "all strategies" (no filter). Plain integer = one
// strategy. Persisted to sessionStorage so flipping pages keeps the
// active scope; clears on tab close (per-session, not per-user).
//
// Why a single id, not a list? The operator's expressed mental model
// is "show me one strategy at a time". When a multi-strategy view is
// needed later (cross-strategy P&L compare), the store can shift to
// list shape without breaking callers — null is the "no filter"
// sentinel either way.

const _STRAT_FILTER_KEY = 'ramboq.strategyFilter.v1';
function _readStratFilter() {
  if (!browser) return null;
  try {
    const v = sessionStorage.getItem(_STRAT_FILTER_KEY);
    if (v === null || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch { return null; }
}
export const selectedStrategyId = writable(/** @type {number|null} */ (_readStratFilter()));
if (browser) {
  selectedStrategyId.subscribe((v) => {
    try {
      if (v == null) sessionStorage.removeItem(_STRAT_FILTER_KEY);
      else sessionStorage.setItem(_STRAT_FILTER_KEY, String(v));
    } catch { /* private mode / quota — ignore */ }
  });
}

// ── Strategy open-symbol set ──────────────────────────────────────────
//
// Reactive `Set<string>` of UPPER tradingsymbols with `remaining_qty > 0`
// in the currently-selected strategy's lot ledger. Refreshed
// automatically whenever `selectedStrategyId` changes.
//
// Empty Set when no strategy is selected. Consumer pages filter their
// position/holding rows via `set.has(sym.toUpperCase())`.
//
// Single source — all filter-chip surfaces (Pulse, Dashboard,
// /admin/derivatives, /orders for the symbol-based read path) read
// off this one store so a strategy pick on any of them updates
// every subscriber on the same tick. The fetch hits
// /api/strategies/{id}/lots?include_closed=0 once per pick (not per
// page mount) so cross-page navigation is free.
export const strategyOpenSymbols = writable(/** @type {Set<string>} */ (new Set()));

let _lastResolvedStratSid = /** @type {number|null} */ (null);
if (browser) {
  selectedStrategyId.subscribe(async (sid) => {
    if (sid === _lastResolvedStratSid) return;
    _lastResolvedStratSid = sid;
    if (sid == null) {
      strategyOpenSymbols.set(new Set());
      return;
    }
    try {
      const { fetchStrategyLots } = await import('$lib/api');
      const r = await fetchStrategyLots(sid, { includeClosed: false, limit: 500 });
      const set = new Set();
      for (const lot of (r?.rows || [])) {
        if (lot.symbol) set.add(String(lot.symbol).toUpperCase());
      }
      // Defensive — selectedStrategyId could have changed mid-fetch.
      // Only write the result if the in-flight sid still matches.
      if (_lastResolvedStratSid === sid) {
        strategyOpenSymbols.set(set);
      }
    } catch {
      if (_lastResolvedStratSid === sid) {
        strategyOpenSymbols.set(new Set());
      }
    }
  });
}
// nowStamp clock: update every 60 s; pauses when tab hidden so the timer
// doesn't fire needlessly in the background. Immediate refire on tab
// return ensures the header clock refreshes the moment the operator
// switches back — no stale timestamp for up to 60 s after return.
if (browser) {
  visibleInterval(() => nowStamp.set(clientTimestamp()), 60_000);
}

/** Short DD-MMM HH:MM:SS IST | DD-MMM HH:MM:SS EST/EDT for log entries.
 *  Input: ISO string or Date. EST vs EDT label tracks DST automatically. */
export function logTime(iso) {
  if (!iso) return '';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (isNaN(d)) return '';
  const fmt = (tz) => d.toLocaleString('en-GB', {
    day: '2-digit', month: 'short',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone: tz,
  }).replace(',', '');
  // EST in winter, EDT in summer — derive from the date itself.
  const estTz = d.toLocaleTimeString('en-US', {
    timeZoneName: 'short', timeZone: 'America/New_York',
  }).split(' ').pop();
  return `${fmt('Asia/Kolkata')} IST | ${fmt('America/New_York')} ${estTz}`;
}

/** HH:MM:SS in IST. Compact form for inline log rows; pair with
 *  `logTimeEdt` to render a stacked dual-zone cell. */
export function logTimeIst(iso) {
  if (!iso) return '';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (isNaN(d)) return '';
  return d.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone: 'Asia/Kolkata',
  });
}

/** HH:MM:SS in EST/EDT. Compact form for the second line of a
 *  stacked dual-zone log cell. Tracks DST automatically. */
export function logTimeEdt(iso) {
  if (!iso) return '';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (isNaN(d)) return '';
  return d.toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone: 'America/New_York',
  });
}

/** Stacked IST | EDT span markup for `{@html}` blocks. Single source of
 *  truth for the dual-zone cell — any CSS class change to .log-ts /
 *  .log-ts-ist / .log-ts-sep / .log-ts-edt happens here, not in N
 *  template files. Returns '' for unparseable input. */
export function dualTsHtml(iso) {
  if (!iso) return '';
  const ist = logTimeIst(iso);
  const edt = logTimeEdt(iso);
  if (!ist && !edt) return '';
  return `<span class="log-ts log-ts-inline"><span class="log-ts-ist">${ist || '—'}</span><span class="log-ts-sep">|</span><span class="log-ts-edt">${edt || '—'}</span></span>`;
}

/**
 * Lifespan chip metadata for an agent row.
 *
 * Returns `null` for `persistent` agents (the default — no chip needed).
 * Otherwise returns `{ label, color, tooltip, expired }`:
 *   - `label`   — short text for the chip ("2/3 fires" / "Until 15 Jun · 28d")
 *   - `color`   — 'sky' / 'amber' / 'red' / 'grey' for CSS class selection
 *   - `tooltip` — full state for hover (used / max / expires / last_triggered)
 *   - `expired` — true when budget exhausted AND agent should no longer fire
 *
 * Color progression:
 *   - 'sky'   — fresh (< 50% used / > 7 days remaining)
 *   - 'amber' — getting low (>= 50% used / <= 7 days remaining)
 *   - 'red'   — 1 fire / 1 day remaining (urgent)
 *   - 'grey'  — exhausted / status=completed
 *
 * @param {any} agent
 * @returns {{ label: string, color: 'sky'|'amber'|'red'|'grey', tooltip: string, expired: boolean } | null}
 */
/** @param {boolean} isCompleted */
function _oneShotChip(isCompleted) {
  if (isCompleted) {
    return { label: '1-shot · DONE', color: 'grey', tooltip: 'One-shot agent has fired and auto-completed.', expired: true };
  }
  return { label: '1-shot', color: 'sky', tooltip: 'Fires once and auto-completes.', expired: false };
}

/** @param {any} agent @param {boolean} isCompleted */
function _nFiresChip(agent, isCompleted) {
  const max = Number(agent.lifespan_max_fires) || 0;
  const used = Number(agent.trigger_count) || 0;
  const remaining = Math.max(0, max - used);
  if (isCompleted || remaining === 0) {
    return { label: `${used}/${max} · EXHAUSTED`, color: 'grey', tooltip: `Used ${used} of ${max} fires — auto-completed.`, expired: true };
  }
  let color = /** @type {'sky'|'amber'|'red'} */ ('sky');
  if (remaining === 1)           color = 'red';
  else if (used / max >= 0.5)    color = 'amber';
  return { label: `${used}/${max} fires`, color, tooltip: `${remaining} of ${max} fires remaining.`, expired: false };
}

/** @param {any} agent @param {boolean} isCompleted */
function _untilDateChip(agent, isCompleted) {
  const expiresAt = agent.lifespan_expires_at;
  if (!expiresAt) {
    return { label: 'Until (no date)', color: 'grey', tooltip: 'until_date agent missing lifespan_expires_at — would never expire.', expired: false };
  }
  const exp = new Date(expiresAt);
  const msLeft = exp.getTime() - Date.now();
  const daysLeft = Math.ceil(msLeft / 86_400_000);
  const dateStr = exp.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata' });
  if (isCompleted || msLeft <= 0) {
    return { label: `EXPIRED ${dateStr}`, color: 'red', tooltip: `Expired on ${exp.toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })} IST.`, expired: true };
  }
  let color = /** @type {'sky'|'amber'|'red'} */ ('sky');
  if (daysLeft <= 1)         color = 'red';
  else if (daysLeft <= 7)    color = 'amber';
  return {
    label: `Until ${dateStr} · ${daysLeft}d`,
    color,
    tooltip: `Expires on ${exp.toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })} IST (in ${daysLeft} day${daysLeft === 1 ? '' : 's'}).`,
    expired: false,
  };
}

export function lifespanChip(agent) {
  if (!agent) return null;
  const t = agent.lifespan_type;
  if (!t || t === 'persistent') return null;
  const isCompleted = agent.status === 'completed';
  if (t === 'one_shot')   return _oneShotChip(isCompleted);
  if (t === 'n_fires')    return _nFiresChip(agent, isCompleted);
  if (t === 'until_date') return _untilDateChip(agent, isCompleted);
  return null;
}

/** Parse the leading 'YYYY-MM-DD HH:MM:SS[,ms]' timestamp from a python log line
 *  (treated as UTC) and return short IST|EST. Returns null if not found. */
export function parseLogLineTime(line) {
  const m = line?.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
  if (!m) return null;
  return logTime(`${m[1]}T${m[2]}Z`);
}

/** Same as parseLogLineTime but returns a Date instead of a formatted string.
 *  Used when callers want to apply a different render (e.g. formatDualTz). */
export function parseLogLineDate(line) {
  const m = line?.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
  if (!m) return null;
  const d = new Date(`${m[1]}T${m[2]}Z`);
  return isNaN(d.getTime()) ? null : d;
}

// ── Last refresh timestamp ───────────────────────────────────────────
// Single global "when did the last page-data refresh succeed?" ms epoch.
// Surfaced inside every RefreshButton's tooltip — formatted via
// `formatDualTz()` to match the page-header wall-clock format exactly
// (e.g. "Sun 30 May · 21:42 IST · 12:12 EDT"). Updated automatically
// when a RefreshButton sees its `loading` prop fall true → false
// (manual click + any auto-refresh that flows through the same
// `loading` flag), plus direct `.set()` calls from pages whose
// auto-pollers don't go through RefreshButton (loadHero on dashboard,
// loadPulse inside MarketPulse).
export const lastRefreshAt = writable(0);

// ── Include-Holdings toggle (shared with derivatives page) ──────────────
// Backing localStorage key: 'opt.includeHoldings' (same as the
// derivatives page's existing key). Both surfaces read/write this store
// so a toggle on either propagates immediately. When true, NavStrip's
// P pill slots 1 + 2 include the sum of holdings pnl + day_change_val;
// when false they show F&O-only. Mirrors the derivatives payoff overlay
// pattern where _includeHoldings gates equity leg contributions. Operator
// 2026-07-01: "p & l should include underlying position if hold button
// is on. it should exclude underlying position when hold is off. it is
// similar to exp p & L."
const _HOLDINGS_KEY = 'opt.includeHoldings';
function _readHoldingsFlag() {
  if (!browser) return false;
  try {
    const raw = localStorage.getItem(_HOLDINGS_KEY);
    return raw === '1' || raw === 'true';
  } catch { return false; }
}
export const includeHoldings = writable(_readHoldingsFlag());
if (browser) {
  includeHoldings.subscribe((v) => {
    try { localStorage.setItem(_HOLDINGS_KEY, v ? '1' : '0'); } catch { /* quota */ }
  });
  // Cross-tab sync — a toggle on the derivatives page propagates to
  // NavStrip in other tabs on the next storage event.
  window.addEventListener('storage', (e) => {
    if (e.key === _HOLDINGS_KEY) {
      includeHoldings.set(e.newValue === '1' || e.newValue === 'true');
    }
  });
}

// ── Activity-modal control ───────────────────────────────────────────
// Single mount point lives in (algo)/+layout.svelte. Anything that
// wants to open the activity log surfaces (page header Log button,
// navbar broker-status chip, future deep-links) writes to this store
// instead of mounting a second ActivityLogModal.
//
// Shape: { open: bool }
//   Tab selection lives in activityStore (activityStore.svelte.js) so it
//   persists across modal open/close and is shared with the /activity page.
//   initialTab is now written to activityStore.activeTab by openActivityModal;
//   the modal reads activityStore.activeTab directly instead of this field.
/** @typedef {'order'|'agent'|'terminal'|'simulator'|'system'|'conn'|'news'} ActivityTab */

export const activityModal = writable(
  /** @type {{ open: boolean }} */
  ({ open: false })
);

/** Open the activity modal, optionally deep-linking to a specific tab.
 *
 *  When `tab` is provided (e.g. 'conn' from BrokerHealthBadge), it
 *  writes to activityStore so the modal and /activity page land on that
 *  tab immediately. When omitted (the Log button, keyboard `h`, context
 *  menus), the store's current tab is preserved — this is the whole point
 *  of lifting state to activityStore.
 *
 *  @param {ActivityTab} [tab] */
export function openActivityModal(tab) {
  // Only override when a specific deep-link tab was requested.
  // Generic open (no arg) must NOT reset to 'order' or the persistence
  // goal is defeated: user switches to 'agent', closes modal, presses
  // Log again → should re-open on 'agent'.
  if (tab !== undefined) {
    _activityStore.activeTab = tab;
  }
  activityModal.set({ open: true });
}

/** Close the activity modal. */
export function closeActivityModal() {
  activityModal.update((s) => ({ ...s, open: false }));
}

// ── Order-ticket modal control ───────────────────────────────────────
// Keyboard shortcut `t` (trade) writes to this store; PageHeaderActions
// subscribes and calls _openOrder(). Pages without PageHeaderActions
// mounted treat the event as a no-op — consistent with the activity-modal
// pattern.
//
// Shape: {
//   open:    boolean,
//   prefill: OrderTicketPrefill | null
// }
//
// OrderTicketPrefill fields (all optional):
//   symbol        — tradingsymbol string
//   exchange      — 'NSE' | 'NFO' | 'MCX' | 'CDS' | etc.
//   side          — 'BUY' | 'SELL'
//   qty           — raw quantity (equity) or contract qty (F&O)
//   lots          — lot count (F&O); preferred over qty when present
//   price         — limit price hint
//   product       — 'CNC' | 'MIS' | 'NRML'
//   lotSize       — instrument lot size (used by ticket to initialise the lots field)
//   currentQty    — signed held qty (>0 long, <0 short) — drives ADD/CLOSE label
//   action        — 'open' | 'close' | 'modify'
//   account       — account_id hint
//   accounts      — candidate account list
//   triggerSource — 'pulse' | 'derivatives' | 'keyboard' | 'positions' | etc. (audit/debug)
//
// Surfaces that have their own local SymbolPanel instance (MarketPulse,
// derivatives page, performance page, orders page) continue using their
// own local ticketProps — the store path is the global (keyboard / header)
// instance mounted in PageHeaderActions.

/** @typedef {{
 *   symbol?:        string | null,
 *   exchange?:      string | null,
 *   side?:          'BUY' | 'SELL' | null,
 *   qty?:           number | null,
 *   lots?:          number | null,
 *   price?:         number | null,
 *   product?:       'CNC' | 'NRML' | 'MIS' | null,
 *   lotSize?:       number | null,
 *   currentQty?:    number | null,
 *   action?:        'open' | 'close' | 'modify' | 'repeat' | 'cancel' | null,
 *   account?:       string | null,
 *   accounts?:      string[] | null,
 *   triggerSource?: string | null,
 * }} OrderTicketPrefill */

export const orderTicketModal = writable(
  /** @type {{ open: boolean, prefill: OrderTicketPrefill | null }} */
  ({ open: false, prefill: null })
);

/**
 * Open the order-ticket modal from any context.
 *
 * Pass a prefill object to pre-populate the ticket fields.
 * Passing nothing (or null) opens a blank ticket — same as
 * the keyboard `t` shortcut behaviour.
 *
 * @param {OrderTicketPrefill | null} [prefill]
 */
export function openOrderTicketModal(prefill = null) {
  orderTicketModal.set({ open: true, prefill: prefill ?? null });
}
/** Close the order-ticket modal. */
export function closeOrderTicketModal() {
  orderTicketModal.set({ open: false, prefill: null });
}

// ── Chart modal control ──────────────────────────────────────────────
// Mounted once in (algo)/+layout.svelte (same as ActivityLogModal).
// `k` keyboard shortcut + PageHeaderActions chart button both call
// openChartModal(); closeChartModal() is passed as onClose prop.
// Shape: { open: bool, symbol: string, exchange: string }
export const chartModal = writable(
  /** @type {{ open: boolean, symbol: string, exchange: string }} */
  ({ open: false, symbol: '', exchange: '' })
);

/** Open the chart modal, optionally with a pre-selected symbol. */
export function openChartModal(symbol = '', exchange = '') {
  chartModal.set({ open: true, symbol, exchange });
}
/** Close the chart modal. */
export function closeChartModal() {
  chartModal.update((s) => ({ ...s, open: false }));
}

// ── Connection-status store ──────────────────────────────────────────
// Single global health snapshot surfaced as a badge + tooltip on every
// RefreshButton. Polled every 15 s — auto-retries forever via
// visibleInterval, so a backend or broker outage clears on its own as
// soon as connectivity returns.
//
// Shape: {
//   loaded: number,           // count of broker accounts in the live registry
//   total:  number,           // count of broker accounts the operator configured
//   backendOk: boolean,       // true when the last poll succeeded
//   failingAccounts: string[] // account codes for rows where loaded === false
// }
//
// Visual encoding on RefreshButton:
//   backendOk: false              → grey badge with `?`  (API unreachable)
//   backendOk: true, loaded < tot → red/amber + count    (broker offline)
//   backendOk: true, loaded === tot → green + count       (all healthy)
//   total === 0                   → no badge             (demo / no config)
// Operator: "connection chip is updating last. it should be updated
// along with the user id and role chip in navbar." The user pill
// renders synchronously from sessionStorage on initial paint; the
// conn chip was gated on `$connStatus.total > 0` and waited a full
// poll round-trip (~200ms cold, up to 15s on login → next-tick),
// so the chip flashed in late.
//
// Fix: persist the last good `connStatus` to localStorage. On
// store init, restore from that snapshot — chip paints alongside
// the user pill from the very first frame. The fresh poll still
// runs from onMount and overwrites once the new data lands.
const _CONN_LS_KEY = 'rbq.cache.connStatus.v1';
function _readConnStatusLS() {
  if (!browser) return null;
  try {
    const raw = localStorage.getItem(_CONN_LS_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw);
    // Validate shape so a stale / malformed entry can't crash the
    // chip's @const block.
    if (typeof v?.loaded === 'number' && typeof v?.total === 'number'
        && typeof v?.backendOk === 'boolean'
        && Array.isArray(v?.failingAccounts) && Array.isArray(v?.accounts)) {
      return v;
    }
  } catch { /* malformed JSON — ignore, fall through to default */ }
  return null;
}
const _CONN_DEFAULT = { loaded: 0, total: 0, backendOk: true, failingAccounts: [], accounts: [] };
export const connStatus = writable(/** @type {{
  loaded: number,
  total: number,
  backendOk: boolean,
  failingAccounts: string[],
  accounts: string[],
}} */ (_readConnStatusLS() ?? _CONN_DEFAULT));
// Mirror writes back to localStorage. Only persist when we have real
// data (total > 0) so the cached snapshot survives a hard reload
// without flashing the empty-state.
if (browser) {
  connStatus.subscribe((v) => {
    try {
      if (v && v.total > 0) {
        localStorage.setItem(_CONN_LS_KEY, JSON.stringify(v));
      }
    } catch { /* quota / privacy mode — non-fatal */ }
  });
}

let _connPollerStarted = false;
let _connPollerTeardown = null;
let _connPoll = /** @type {(() => Promise<void>) | null} */ (null);
export function startConnStatusPoller() {
  if (!browser || _connPollerStarted) return;
  _connPollerStarted = true;
  const poll = async () => {
    // Anonymous demo session — /api/admin/brokers is admin-guarded
    // and would 401, then the catch block below would flip
    // backendOk=false and paint the misleading grey `?` badge on
    // every page. Operator: "on TV the refresh icon is showing ?
    // instead of 5." The TV browser has no JWT, so we short-circuit:
    // reset to the "no badge" state (`total=0, backendOk=true`) and
    // skip the fetch entirely. The badge stays hidden — same UX as
    // a fresh install with no broker config.
    if (!authStore.getToken()) {
      // Anonymous demo session — /api/admin/brokers is admin-guarded so we
      // skip it (avoids 401 flipping backendOk=false and showing the grey `?`
      // badge). Instead, fetch the masked account list from /api/accounts/
      // which is served to demo sessions with masking applied (ZG####, DH####,
      // etc.). Keeping total=0 preserves the "no badge" contract so
      // the RefreshButton chip stays hidden for anonymous viewers.
      //
      // Defense-in-depth: purge any prior-admin localStorage snapshot so real
      // account IDs from a prior admin session don't paint on the first frame
      // before this fetch resolves.
      try { localStorage.removeItem(_CONN_LS_KEY); } catch { /* non-fatal */ }
      try {
        const { fetchAccounts } = await import('$lib/api');
        const data = await fetchAccounts();
        const accountCodes = (data?.accounts || [])
          .map((a) => String(a?.account_id || ''))
          .filter(Boolean);
        connStatus.set({
          loaded: 0, total: 0, backendOk: true,
          failingAccounts: [], accounts: accountCodes,
        });
      } catch {
        // Non-fatal — demo still shows positions/holdings accounts from rows.
        connStatus.set({
          loaded: 0, total: 0, backendOk: true,
          failingAccounts: [], accounts: [],
        });
      }
      return;
    }
    try {
      const { fetchBrokerAccounts } = await import('$lib/api');
      const accounts = await fetchBrokerAccounts();
      if (!Array.isArray(accounts)) {
        // 200 OK but unexpected payload — treat as backend issue but
        // keep last known broker state so the badge doesn't flicker.
        connStatus.update((v) => ({ ...v, backendOk: false }));
        return;
      }
      const failing = accounts
        .filter((a) => a && !a.loaded)
        .map((a) => String(a.account || ''))
        .filter(Boolean);
      const allCodes = accounts
        .filter((a) => a?.account)
        .map((a) => String(a.account));
      connStatus.set({
        total:  accounts.length,
        loaded: accounts.filter((a) => a?.loaded).length,
        backendOk: true,
        failingAccounts: failing,
        accounts: allCodes,
      });
    } catch {
      // Fetch rejected → API unreachable. Mark backendOk=false but
      // keep the prior `loaded / total / failingAccounts` so the
      // operator can still see what was running BEFORE the outage.
      connStatus.update((v) => ({ ...v, backendOk: false }));
    }
  };
  _connPoll = poll;
  poll();
  // Throttle to 60 s on hidden (industry-standard hybrid): broker
  // health must stay monitored even in the background, but a 15 s
  // cadence when the operator isn't looking is wasteful. On tab
  // return the immediate refire restores the live chip without lag.
  _connPollerTeardown = visibleInterval(poll, 15000, 'throttle:60000');
  // Re-fire on every authStore transition so the chip updates in
  // lock-step with the user / role pill on login, impersonation,
  // and impersonation-end — instead of waiting for the next 15 s
  // poll tick. The first subscription event fires synchronously
  // and is skipped (it duplicates the `poll()` call above).
  let _firstAuthFire = true;
  authStore.subscribe(() => {
    if (_firstAuthFire) { _firstAuthFire = false; return; }
    poll();
  });
}

/** Public hook — re-fires the conn poll immediately. Used by the
 *  /signin form on successful login so the broker chip appears
 *  alongside the user pill on the post-login navigation, without
 *  waiting for the (algo)/+layout onMount to remount the poller. */
export function refreshConnStatusNow() {
  if (browser && _connPoll) _connPoll();
}

function stopConnStatusPoller() {
  if (_connPollerTeardown) {
    _connPollerTeardown();
    _connPollerTeardown = null;
  }
  _connPollerStarted = false;
}

// Holiday-aware market status poller — fetches /api/market/status every
// 5 min so isNseOpen / isMcxOpen / isMarketOpen return the correct value
// on Republic Day, Diwali, etc. (where weekday+time alone would falsely
// say "open"). The RefreshButton "Both NSE and MCX are currently closed"
// popup depends on this. Started from the (algo)/+layout onMount.
let _mktStatusStarted = false;
/** @type {(() => void) | null} */
let _mktStatusTeardown = null;
export function startMarketStatusPoller() {
  if (!browser || _mktStatusStarted) return;
  _mktStatusStarted = true;
  fetchMarketStatus();
  // Market status is holiday-calendar data that changes at most once a
  // day. Throttle to 5 min on hidden (same cadence, effectively no-op
  // reduction) but inherit the immediate-refire-on-return contract so
  // the RefreshButton "closed" popup is accurate as soon as the
  // operator switches back to the tab.
  _mktStatusTeardown = visibleInterval(fetchMarketStatus, 5 * 60 * 1000, 'throttle:300000');
}

function stopMarketStatusPoller() {
  if (_mktStatusTeardown) {
    _mktStatusTeardown();
    _mktStatusTeardown = null;
  }
  _mktStatusStarted = false;
}

// ---------------------------------------------------------------------------
// Broker auth-health store — drives chip color + BrokerHealthBadge popup.
//
// Polls GET /api/admin/broker-health every 30 s (visibleInterval).
// NOT market-gated — auth breaks happen outside market hours.
// Singleton — safe to start from layout; BrokerHealthBadge reads from here
// instead of maintaining its own _rawAccounts fetch, eliminating duplicate
// polling and ensuring the chip color stays fresh continuously.
//
// Shape: { accounts: BrokerAccountHealth[], worstState: 'green'|'amber'|'red' }
//
// worstState priority: red > amber > green.
// Falls through to 'amber' (never grey) when no data loaded yet or all
// accounts have unknown state.
// ---------------------------------------------------------------------------

/** @typedef {{ account: string, broker: string, state: string, reason: string, last_good_at: string|null, last_check_at: string|null, is_active_ticker?: boolean, circuit_state?: string, consecutive_fail_count?: number, circuit_open_until?: string|null, circuit_breaker_enabled?: boolean, poll_priority?: string, auto_downgrade_enabled?: boolean, auto_downgraded_at?: string|null, auto_downgrade_reason?: string|null }} BrokerAccountHealth */

/**
 * Derive worst state across all accounts.
 * red > amber > green; returns 'amber' for empty (never grey).
 * @param {BrokerAccountHealth[]} accounts
 * @returns {'green'|'amber'|'red'}
 */
function _brokerHealthWorstState(accounts) {
  if (!accounts || accounts.length === 0) return 'amber';
  if (accounts.some(a => a.state === 'red'))      return 'red';
  if (accounts.some(a => a.state === 'amber'))    return 'amber';
  if (accounts.some(a => a.state === 'inactive')) return 'amber';
  if (accounts.every(a => a.state === 'green'))   return 'green';
  return 'amber';
}

export const brokerHealthStore = writable(/** @type {{ accounts: BrokerAccountHealth[], worstState: 'green'|'amber'|'red' }} */ ({
  accounts: [],
  worstState: 'amber',
}));

let _bhPollerStarted = false;
/** @type {(() => void) | null} */
let _bhPollerTeardown = null;

export function startBrokerHealthPoller() {
  if (!browser || _bhPollerStarted) return;
  _bhPollerStarted = true;
  const poll = async () => {
    if (!authStore.getToken()) return;   // anonymous — skip (admin-guarded endpoint)
    try {
      const { fetchBrokerHealth } = await import('$lib/api');
      const data = await fetchBrokerHealth();
      const accounts = /** @type {BrokerAccountHealth[]} */ (data?.accounts ?? []);
      brokerHealthStore.set({ accounts, worstState: _brokerHealthWorstState(accounts) });
    } catch {
      // Silently suppress — store keeps prior state so chip stays colored.
    }
  };
  poll();
  // 30 s cadence; throttle to 60 s on hidden so we keep monitoring
  // auth breaks in the background without burning unnecessary quota.
  _bhPollerTeardown = visibleInterval(poll, 30_000, 'throttle:60000');
}
