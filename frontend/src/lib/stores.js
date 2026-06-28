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
import { isMarketOpen, fetchMarketStatus } from '$lib/marketHours';

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
  holdings:  null,   // { rows, summary, refreshed_at }
  positions: null,   // { rows, summary, refreshed_at }
  funds:     null,   // { rows, refreshed_at }
  insights:  null,   // { content }
};

/**
 * Compact page-top timestamp banner. Day-first (20 Apr) to match
 * Indian / British convention; 3-letter weekday + 3-letter month;
 * year dropped; 24-hour time. Example:
 *   "Mon 20 Apr  23:06 IST | Mon 20 Apr  13:36 EDT"
 * Both date halves repeated so IST/EST day-boundary cases stay
 * unambiguous. Auto-resolves EST/EDT by season.
 */
/**
 * setInterval variant that pauses its callback while the browser tab is
 * backgrounded. Use this for every algo-page polling interval — it cuts
 * the per-tab HTTP noise on Dashboard / Agents / Orders / Simulator /
 * Terminal to zero when the user switches away.
 *
 * Returns a teardown function: call it from onDestroy (no separate
 * clearInterval needed).
 *
 * @param {() => void} fn   callback to run on each tick
 * @param {number}      ms  interval in milliseconds
 * @returns {() => void}    teardown; clears the interval + removes the listener
 */
export function visibleInterval(fn, ms) {
  let id = null;
  const start = () => { if (id == null) id = setInterval(fn, ms); };
  const stop  = () => { if (id != null) { clearInterval(id); id = null; } };
  const onVis = () => { document.hidden ? stop() : start(); };
  if (typeof document !== 'undefined') {
    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVis);
  } else {
    start();
  }
  return () => {
    stop();
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVis);
    }
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
 * The visibility gate (pause when tab hidden) layers on top, same as
 * `visibleInterval`. Manual refresh buttons keep working always —
 * operators clicking a button are explicitly asking for a fetch.
 *
 * @param {() => void} fn   callback to run on each tick
 * @param {number}      ms  interval in milliseconds
 * @returns {() => void}    teardown
 */
export function marketAwareInterval(fn, ms) {
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
  const mainTeardown = visibleInterval(() => {
    if (!isMarketOpen()) return;
    fn();
  }, ms);
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
if (typeof setInterval !== 'undefined') {
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
export function formatDualTz(/** @type {Date} */ date) {
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
if (browser) {
  setInterval(() => nowStamp.set(clientTimestamp()), 60_000);
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
 *   - 'amber' — getting low (≥ 50% used / ≤ 7 days remaining)
 *   - 'red'   — 1 fire / 1 day remaining (urgent)
 *   - 'grey'  — exhausted / status=completed
 *
 * @param {any} agent
 * @returns {{ label: string, color: 'sky'|'amber'|'red'|'grey', tooltip: string, expired: boolean } | null}
 */
export function lifespanChip(agent) {
  if (!agent) return null;
  const t = agent.lifespan_type;
  if (!t || t === 'persistent') return null;
  const isCompleted = agent.status === 'completed';

  if (t === 'one_shot') {
    if (isCompleted) {
      return {
        label: '1-shot · DONE',
        color: 'grey',
        tooltip: 'One-shot agent has fired and auto-completed.',
        expired: true,
      };
    }
    return {
      label: '1-shot',
      color: 'sky',
      tooltip: 'Fires once and auto-completes.',
      expired: false,
    };
  }

  if (t === 'n_fires') {
    const max = Number(agent.lifespan_max_fires) || 0;
    const used = Number(agent.trigger_count) || 0;
    const remaining = Math.max(0, max - used);
    if (isCompleted || remaining === 0) {
      return {
        label: `${used}/${max} · EXHAUSTED`,
        color: 'grey',
        tooltip: `Used ${used} of ${max} fires — auto-completed.`,
        expired: true,
      };
    }
    let color = /** @type {'sky'|'amber'|'red'} */ ('sky');
    if (remaining === 1)             color = 'red';
    else if (used / max >= 0.5)      color = 'amber';
    return {
      label: `${used}/${max} fires`,
      color,
      tooltip: `${remaining} of ${max} fires remaining.`,
      expired: false,
    };
  }

  if (t === 'until_date') {
    const expiresAt = agent.lifespan_expires_at;
    if (!expiresAt) {
      return {
        label: 'Until (no date)',
        color: 'grey',
        tooltip: 'until_date agent missing lifespan_expires_at — would never expire.',
        expired: false,
      };
    }
    const exp = new Date(expiresAt);
    const now = new Date();
    const msLeft = exp.getTime() - now.getTime();
    const daysLeft = Math.ceil(msLeft / 86_400_000);
    const dateStr = exp.toLocaleDateString('en-GB', {
      day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata',
    });
    if (isCompleted || msLeft <= 0) {
      return {
        label: `EXPIRED ${dateStr}`,
        color: 'red',
        tooltip: `Expired on ${exp.toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })} IST.`,
        expired: true,
      };
    }
    let color = /** @type {'sky'|'amber'|'red'} */ ('sky');
    if (daysLeft <= 1)               color = 'red';
    else if (daysLeft <= 7)          color = 'amber';
    return {
      label: `Until ${dateStr} · ${daysLeft}d`,
      color,
      tooltip: `Expires on ${exp.toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })} IST (in ${daysLeft} day${daysLeft === 1 ? '' : 's'}).`,
      expired: false,
    };
  }

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

// ── Activity-modal control ───────────────────────────────────────────
// Single mount point lives in (algo)/+layout.svelte. Anything that
// wants to open the activity log surfaces (page header Log button,
// navbar broker-status chip, future deep-links) writes to this store
// instead of mounting a second ActivityLogModal.
//
// Shape: { open: bool, initialTab: 'order'|'agent'|'simulator'|'system'|'conn'|'news' }
//   initialTab — which LogPanel tab to land on. The navbar broker chip
//                sets 'conn' so the operator drops straight into the
//                conn_service log; the Log button uses the default 'order'.
/** @typedef {'order'|'agent'|'terminal'|'simulator'|'system'|'conn'|'news'} ActivityTab */

export const activityModal = writable(
  /** @type {{ open: boolean, initialTab: ActivityTab }} */
  ({ open: false, initialTab: /** @type {ActivityTab} */ ('order') })
);

/** Open the activity modal pre-selected on a given LogPanel tab.
 *  @param {ActivityTab} [initialTab] */
export function openActivityModal(initialTab = 'order') {
  activityModal.set({ open: true, initialTab });
}

/** Close the activity modal. */
export function closeActivityModal() {
  activityModal.update((s) => ({ ...s, open: false }));
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
      connStatus.set({
        loaded: 0, total: 0, backendOk: true,
        failingAccounts: [], accounts: [],
      });
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
  _connPollerTeardown = visibleInterval(poll, 15000);
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

export function stopConnStatusPoller() {
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
  _mktStatusTeardown = visibleInterval(fetchMarketStatus, 5 * 60 * 1000);
}

export function stopMarketStatusPoller() {
  if (_mktStatusTeardown) {
    _mktStatusTeardown();
    _mktStatusTeardown = null;
  }
  _mktStatusStarted = false;
}
