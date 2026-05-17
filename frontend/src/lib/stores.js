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

import { writable } from 'svelte/store';
import { browser } from '$app/environment';
import { isMarketOpen } from '$lib/marketHours';

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
  if (!browser) return { token: null, user: null };
  try {
    const token = sessionStorage.getItem('ramboq_token');
    const raw   = sessionStorage.getItem('ramboq_user');
    const user  = raw ? JSON.parse(raw) : null;
    if (token && user) {
      const claims = _decodeJwt(token);
      if (claims) {
        // JWT wins for role so a stale `ramboq_user` blob can't pin the
        // role chip wrong. role values: 'partner' | 'admin' | 'designated'.
        user.role = claims.role ?? user.role;
        // Defensive: legacy sessions persisted is_super; drop it.
        delete user.is_super;
      }
    }
    return { token, user };
  } catch {
    return { token: null, user: null };
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
      }
      set({ token, user });
    },

    /** Call on logout or 401. */
    logout() {
      if (browser) {
        sessionStorage.removeItem('ramboq_token');
        sessionStorage.removeItem('ramboq_user');
      }
      set({ token: null, user: null });
    },

    /** Read token directly (non-reactive). */
    getToken() {
      return browser ? sessionStorage.getItem('ramboq_token') : null;
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
  return visibleInterval(() => {
    if (!isMarketOpen()) return;
    fn();
  }, ms);
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
 *  Defaults to 'paper'; will be wired to /api/admin/execution/mode
 *  in a follow-up commit. */
export const executionMode = writable(/** @type {'sim'|'replay'|'paper'|'shadow'|'live'} */ ('paper'));

export function branchLabel(/** @type {string|null|undefined} */ name) {
  if (!name) return '';
  return name === 'main' ? 'prod' : name;
}

export function clientTimestamp() {
  const now = new Date();
  const fmt = (tz) => {
    const parts = new Intl.DateTimeFormat('en-GB', {
      weekday: 'short', day: '2-digit', month: 'short',
      hour: '2-digit', minute: '2-digit', hour12: false,
      timeZone: tz,
    }).formatToParts(now);
    const pick = (t) => (parts.find(p => p.type === t) || {}).value || '';
    return `${pick('weekday')} ${pick('day')} ${pick('month')} ${pick('hour')}:${pick('minute')}`;
  };
  const estTz = now.toLocaleTimeString('en-US', {
    timeZoneName: 'short', timeZone: 'America/New_York',
  }).split(' ').pop();   // "EST" / "EDT" by season
  return `${fmt('Asia/Kolkata')} IST | ${fmt('America/New_York')} ${estTz}`;
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

/** Parse the leading 'YYYY-MM-DD HH:MM:SS[,ms]' timestamp from a python log line
 *  (treated as UTC) and return short IST|EST. Returns null if not found. */
export function parseLogLineTime(line) {
  const m = line?.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
  if (!m) return null;
  return logTime(`${m[1]}T${m[2]}Z`);
}
