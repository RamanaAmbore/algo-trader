/**
 * Frontend market-hours gate for auto-refresh polling.
 *
 * Indian market segments (matches `backend_config.yaml::market_segments`):
 *   NSE equity:    09:15-15:30 IST, Mon-Fri
 *   MCX commodity: 09:00-23:30 IST, Mon-Fri
 *
 * Two sources of truth:
 *   1. Time-of-day (weekday + minute window) — cheap, sync, always-on.
 *      Used as fallback when the server hasn't been polled yet.
 *   2. Backend GET /api/market/status — holiday-aware. The backend
 *      consults the Kite-fetched NSE/MCX holiday calendars + a live
 *      quote-probe override, so the answer is correct on Republic Day,
 *      Diwali, Muhurat sessions, and MCX evening sessions on equity
 *      holidays. Polled every 5 min by startMarketStatusPoller() in
 *      stores.js; cached in this module so isNseOpen/isMcxOpen stay
 *      synchronous for tight render loops.
 *
 * Manual refresh buttons (the ↻ on /admin/options, the Refresh on
 * /performance, etc.) bypass the marketAwareInterval gate by design —
 * operators clicking a button are explicitly asking for a fresh fetch.
 * They DO consult the server-state for the "market closed" popup so
 * the popup fires correctly on Indian-market holidays.
 */

import { browser } from '$app/environment';

// Minute-of-day boundaries (IST).
const NSE_OPEN_MIN  = 9 * 60 + 15;   // 09:15
const NSE_CLOSE_MIN = 15 * 60 + 30;  // 15:30
const MCX_OPEN_MIN  = 9 * 60;        // 09:00
const MCX_CLOSE_MIN = 23 * 60 + 30;  // 23:30
const ANY_OPEN_MIN  = MCX_OPEN_MIN;
const ANY_CLOSE_MIN = MCX_CLOSE_MIN;

const _WD_MAP = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

// Last server-side market status (holiday-aware). Null until the first
// poll completes. Once set, isNseOpen/isMcxOpen prefer this over the
// time-of-day calculation.
/** @type {{ nse_open: boolean, mcx_open: boolean, any_open: boolean, is_holiday: boolean, _at: number } | null} */
let _serverStatus = null;

/** Resolve current IST weekday + minute-of-day from a Date. */
function _istNow(/** @type {Date} */ now) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'Asia/Kolkata',
  }).formatToParts(now);
  const pick = (t) => (parts.find(p => p.type === t) || {}).value || '';
  const hh = parseInt(pick('hour'), 10) || 0;
  const mm = parseInt(pick('minute'), 10) || 0;
  return {
    weekday: _WD_MAP[pick('weekday')] ?? 0,
    minute:  hh * 60 + mm,
  };
}

/**
 * Push a fresh status from the backend poller (called by stores.js).
 * Stale-but-present is always preferred over absent — even an hour-old
 * status is better than the time-only fallback on a holiday.
 *
 * @param {{ nse_open: boolean, mcx_open: boolean, any_open: boolean, is_holiday: boolean }} status
 */
export function setServerMarketStatus(status) {
  if (!status || typeof status !== 'object') return;
  _serverStatus = { ...status, _at: Date.now() };
}

/** Any market segment open. Holiday-aware when the server poll has landed. */
export function isMarketOpen(/** @type {Date} */ now = new Date()) {
  if (_serverStatus) return _serverStatus.any_open;
  const { weekday, minute } = _istNow(now);
  if (weekday === 0 || weekday === 6) return false;
  return minute >= ANY_OPEN_MIN && minute <= ANY_CLOSE_MIN;
}

/** NSE equity window. Holiday-aware when the server poll has landed. */
export function isNseOpen(/** @type {Date} */ now = new Date()) {
  if (_serverStatus) return _serverStatus.nse_open;
  const { weekday, minute } = _istNow(now);
  if (weekday === 0 || weekday === 6) return false;
  return minute >= NSE_OPEN_MIN && minute <= NSE_CLOSE_MIN;
}

/** MCX commodity window. Holiday-aware when the server poll has landed. */
export function isMcxOpen(/** @type {Date} */ now = new Date()) {
  if (_serverStatus) return _serverStatus.mcx_open;
  const { weekday, minute } = _istNow(now);
  if (weekday === 0 || weekday === 6) return false;
  return minute >= MCX_OPEN_MIN && minute <= MCX_CLOSE_MIN;
}

/** True iff the server flagged today as an NSE-or-MCX holiday. False until
 *  the first poll lands (so callers fall back to "not flagged" rather than
 *  blocking on the network). */
export function isMarketHoliday() {
  return !!(_serverStatus && _serverStatus.is_holiday);
}

/**
 * One-shot fetch of /api/market/status. Stores.js wires this into a
 * 5-min visibleInterval. Resolves silently on failure so a backend
 * outage just leaves the time-only fallback active.
 */
export async function fetchMarketStatus() {
  if (!browser) return;
  try {
    const r = await fetch('/api/market/status', { credentials: 'include' });
    if (!r.ok) return;
    const j = await r.json();
    setServerMarketStatus(j);
  } catch { /* silent — fallback stays active */ }
}
