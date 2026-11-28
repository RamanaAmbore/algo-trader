/**
 * Frontend market-hours gate for auto-refresh polling.
 *
 * Indian market segments (matches `backend_config.yaml::market_segments`):
 *   NSE equity:    09:15-15:30 IST, Mon-Fri
 *   MCX commodity: 09:00-23:30/23:55 IST, Mon-Fri — the evening close is
 *     DST-aware (23:30 while US DST is active, 23:55 during US winter/
 *     standard time; see `./data/mcxCloseTime.js` and the backend's
 *     `mcx_non_agri_close_time()`), NOT a flat 23:30.
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
import { mcxNonAgriCloseMinuteOfDay } from './data/mcxCloseTime.js';

// Minute-of-day boundaries (IST).
const NSE_OPEN_MIN  = 9 * 60 + 15;   // 09:15
const NSE_CLOSE_MIN = 15 * 60 + 30;  // 15:30
const MCX_OPEN_MIN  = 9 * 60 + 0;    // 09:00 (reverted 2026-10-22 — matches
                                      // backend_config.yaml::market_segments.
                                      // commodity.sessions; MCX's own official
                                      // circular, MCX/TRD/068/2026, confirms
                                      // non-agri commodities trade from 09:00
                                      // IST. A same-day 09:30 change earlier
                                      // was wrong and has been reverted.)
// MCX's evening close is DST-aware (23:30 while US DST is active, 23:55
// during US winter/standard time) — NOT a flat 23:30. Resolved per-date
// via mcxNonAgriCloseMinuteOfDay() (./data/mcxCloseTime.js), the frontend
// mirror of the backend's mcx_non_agri_close_time() (derivatives.py). Do
// NOT reintroduce a flat `23 * 60 + 30` constant here — that was the
// exact bug this module is fixed to avoid (see CLAUDE.md "MCX DST-aware
// non-agri close time").
// MCX opens first (09:00), NSE second (09:15) — ANY_OPEN_MIN is the earlier
// of the two rather than a hardcoded alias to either, so a future change to
// either segment's open time can't silently desync this derived constant.
const ANY_OPEN_MIN  = Math.min(NSE_OPEN_MIN, MCX_OPEN_MIN);

const _WD_MAP = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

// Last server-side market status (holiday-aware). Null until the first
// poll completes. Once set, isNseOpen/isMcxOpen prefer this over the
// time-of-day calculation.
/** @type {{ nse_open: boolean, mcx_open: boolean, any_open: boolean, is_holiday: boolean, _at: number } | null} */
let _serverStatus = null;

/**
 * Resolve current IST weekday + minute-of-day + calendar-date parts
 * from a Date. The year/month/day fields are needed so callers can
 * resolve MCX's DST-aware close time per-date via
 * `mcxNonAgriCloseMinuteOfDay()` — a bare minute-of-day isn't enough to
 * know whether US DST is active for the date in question.
 */
function _istNow(/** @type {Date} */ now) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    weekday: 'short', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'Asia/Kolkata',
  }).formatToParts(now);
  const pick = (t) => (parts.find(p => p.type === t) || {}).value || '';
  const hh = parseInt(pick('hour'), 10) || 0;
  const mm = parseInt(pick('minute'), 10) || 0;
  return {
    weekday: _WD_MAP[pick('weekday')] ?? 0,
    minute:  hh * 60 + mm,
    year:    parseInt(pick('year'), 10) || 1970,
    month:   parseInt(pick('month'), 10) || 1,
    day:     parseInt(pick('day'), 10) || 1,
  };
}

/** MCX's DST-aware IST close minute-of-day for the IST calendar date
 *  carried by an `_istNow()` result. */
function _mcxCloseMinFor(/** @type {{year: number, month: number, day: number}} */ parts) {
  return mcxNonAgriCloseMinuteOfDay(parts.year, parts.month, parts.day);
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
  for (const cb of _statusListeners) {
    try { cb(); } catch { /* one bad listener must not break the others */ }
  }
}

/** @type {Set<() => void>} */
const _statusListeners = new Set();

/**
 * Register a callback fired synchronously every time a fresh poll result
 * lands via setServerMarketStatus(). Lets a consumer recompute immediately
 * instead of waiting for its own next timer tick — closes a real race
 * where a component mounts and seeds from the time-of-day fallback a few
 * hundred ms before the in-flight /api/market/status poll resolves (the
 * component would otherwise show a stale value until its next tick, up to
 * 30s later). Returns an unsubscribe function.
 *
 * @param {() => void} cb
 * @returns {() => void}
 */
export function onMarketStatusChange(cb) {
  _statusListeners.add(cb);
  return () => { _statusListeners.delete(cb); };
}

/** Any market segment open. Holiday-aware when the server poll has landed. */
export function isMarketOpen(/** @type {Date} */ now = new Date()) {
  if (_serverStatus) return _serverStatus.any_open;
  const parts = _istNow(now);
  const { weekday, minute } = parts;
  if (weekday === 0 || weekday === 6) return false;
  // MCX closes last of the two segments, so its (DST-aware) close is
  // the "any segment open" upper bound.
  return minute >= ANY_OPEN_MIN && minute <= _mcxCloseMinFor(parts);
}

/** NSE equity window. Holiday-aware when the server poll has landed. */
export function isNseOpen(/** @type {Date} */ now = new Date()) {
  if (_serverStatus) return _serverStatus.nse_open;
  const { weekday, minute } = _istNow(now);
  if (weekday === 0 || weekday === 6) return false;
  return minute >= NSE_OPEN_MIN && minute <= NSE_CLOSE_MIN;
}

/** MCX commodity window. Holiday-aware when the server poll has landed.
 *  Close time is DST-aware (23:30 in-DST, 23:55 winter) — see
 *  `_mcxCloseMinFor()` / `./data/mcxCloseTime.js`. */
export function isMcxOpen(/** @type {Date} */ now = new Date()) {
  if (_serverStatus) return _serverStatus.mcx_open;
  const parts = _istNow(now);
  const { weekday, minute } = parts;
  if (weekday === 0 || weekday === 6) return false;
  return minute >= MCX_OPEN_MIN && minute <= _mcxCloseMinFor(parts);
}

/** True iff the server flagged today as an NSE-or-MCX holiday. False until
 *  the first poll lands (so callers fall back to "not flagged" rather than
 *  blocking on the network). */
export function isMarketHoliday() {
  return !!(_serverStatus && _serverStatus.is_holiday);
}

// ── Book-poller snapshot grace windows ──────────────────────────────────
//
// Consumed by `_tickBookPollers()` in
// `frontend/src/lib/data/marketDataStores.svelte.js`. Two narrow windows
// where the backend's `daily_book` genuinely changes even though
// isNseOpen()/isMcxOpen() are both false, per CLAUDE.md's "Market daily
// window" section:
//   - 08:00 IST: fix_daily_book_prev_close() resets prev_close for the new
//     session (the only moment prev_close changes outside a close
//     snapshot). Window runs until the earlier of NSE/MCX open (MCX,
//     09:00) — once either segment is open, the live
//     isNseOpen()/isMcxOpen() path already fetches, so the grace window
//     only needs to bridge 08:00→open.
//   - MCX close + 15 min: the MCX settlement snapshot write lands 15min
//     after MCX's real (DST-aware) close; +5min buffer absorbs
//     write/cron jitter. The NSE-only 15:45 close snapshot needs no
//     separate window of its own — MCX is still open at 15:45 (closes
//     23:30/23:55), so the live isMcxOpen() path already covers that
//     fetch.
// Outside these two windows the fully-closed span (overnight, weekends,
// holiday clusters) is genuinely static — `daily_book` cannot change
// again until the next of these two writes — so there is nothing worth
// fetching.
const _BOOK_PREMARKET_GRACE_START_MIN = 8 * 60;  // 08:00
const _BOOK_POSTCLOSE_GRACE_BUFFER_MIN = 20;     // close+15min write + 5min buffer

/**
 * True iff `minute` falls inside [closeMin, closeMin+20) on the SAME
 * calendar day, clamped to end-of-day — i.e. the part of the post-close
 * grace window that doesn't cross midnight. On a DST-window date
 * (23:30 close) this never wraps. On a winter-window date (23:55 close)
 * this covers only 23:55→23:59:59; the 00:00→00:15 spillover into the
 * NEXT calendar day is handled separately by the wrap check below.
 */
function _postCloseGraceSameDay(/** @type {number} */ minute, /** @type {number} */ closeMin) {
  const graceEnd = Math.min(closeMin + _BOOK_POSTCLOSE_GRACE_BUFFER_MIN, 24 * 60);
  return minute >= closeMin && minute < graceEnd;
}

/**
 * True iff `minute` (today's own minute-of-day) falls inside the part of
 * YESTERDAY's post-close grace window that spilled past midnight —
 * e.g. a winter-window 23:55 close + 20min = 00:15 the next IST day.
 * Mirrors the backend's "check yesterday's wrapped session too" rule
 * (`exchange_clock.py:is_market_active_for_prev_close` /
 * `_session_covers_instant`), which exists for the exact same reason:
 * a post-close window anchored to a close time can land after
 * midnight and a same-day-only check would miss it.
 */
function _postCloseGraceWrapFromYesterday(/** @type {number} */ minute, /** @type {number} */ yesterdayCloseMin) {
  const graceEnd = yesterdayCloseMin + _BOOK_POSTCLOSE_GRACE_BUFFER_MIN;
  if (graceEnd <= 24 * 60) return false; // no wrap on a DST-window (23:30) date
  const wrapEnd = graceEnd - 24 * 60;
  return minute < wrapEnd;
}

/**
 * True only inside the premarket (08:00→market open) or post-MCX-close
 * (DST-aware close→close+20min) snapshot-write windows described above.
 * Book-poller specific — NOT a general "market about to open" signal;
 * do not reuse for other polling decisions. Weekday + holiday-aware via
 * isMarketHoliday() (conservative default: not-a-holiday until the first
 * server poll lands, matching the rest of this module).
 */
export function isBookSnapshotGraceWindow(/** @type {Date} */ now = new Date()) {
  if (isMarketHoliday()) return false;
  const parts = _istNow(now);
  const { weekday, minute } = parts;
  if (weekday !== 0 && weekday !== 6) {
    if (minute >= _BOOK_PREMARKET_GRACE_START_MIN && minute < ANY_OPEN_MIN) return true;
    if (_postCloseGraceSameDay(minute, _mcxCloseMinFor(parts))) return true;
  }
  // Winter-window wrap check: yesterday's own close+20 can spill past
  // midnight into today's first few minutes. Uses YESTERDAY's IST
  // calendar date/weekday — exactly 24h back, which is always safe in
  // Asia/Kolkata since IST observes no DST of its own.
  const yParts = _istNow(new Date(now.getTime() - 24 * 60 * 60 * 1000));
  if (yParts.weekday !== 0 && yParts.weekday !== 6) {
    if (_postCloseGraceWrapFromYesterday(minute, _mcxCloseMinFor(yParts))) return true;
  }
  return false;
}

/**
 * Per-exchange open/closed resolution — Chain-tab counterpart to
 * OrderTicket.svelte's own `_mktOpenForExchange()`. Deliberately a
 * SEPARATE, duplicated function rather than OrderTicket delegating to
 * this one (or vice versa): `e2e/order_ticket_market_badge_and_error_toast.spec.js`
 * asserts against the exact literal source text of OrderTicket's own
 * `_mktOpenForExchange` (its branches, its imports) — refactoring that
 * function to call out to this module would change the source text the
 * spec regex-matches against for no behavioural benefit. Keep the two
 * mapping tables in sync by hand if the backend's
 * `_EXCHANGE_TO_SEGMENT` table (agent_engine.py) ever changes.
 *
 * Mapping: MCX/NCO → isMcxOpen() (commodity segment); NFO/NSE/BSE/BFO/
 * CDS/BCD → isNseOpen() (equity segment); any other/unrecognized
 * exchange code → false (fail-closed, matching the backend's
 * "unknown segment → never wrongly allow" convention).
 *
 * @param {string} exch — already-uppercased exchange code
 * @returns {boolean}
 */
export function segmentOpenForExchange(exch) {
  if (exch === 'MCX' || exch === 'NCO') return isMcxOpen();
  if (exch === 'NFO' || exch === 'NSE' || exch === 'BSE' || exch === 'BFO' || exch === 'CDS' || exch === 'BCD') return isNseOpen();
  return false;
}

/**
 * Defaults to the real global `setInterval`/`clearInterval`. `stores.js`
 * passes `visibleInterval` instead for `setIntervalFn`, plus a
 * `clearIntervalFn` that invokes the teardown function `visibleInterval`
 * returns — so the shared poll pauses while the tab is hidden and
 * refires immediately on return, the exact behaviour every per-component
 * timer it replaces already had. `setIntervalFn`'s return type is
 * deliberately `any` (not `typeof setInterval`'s numeric handle), since
 * `visibleInterval` returns a teardown function instead.
 *
 * @typedef {object} MarketOpenPollDeps
 * @property {(fn: () => void, ms: number) => any} [setIntervalFn]
 * @property {(handle: any) => void} [clearIntervalFn]
 * @property {number} [intervalMs]
 * @property {(cb: () => void) => () => void} [onStatusChange]
 */

/**
 * Shared market-open poll — a pure, dependency-injectable factory
 * consolidating what used to be four independent 30s `visibleInterval`
 * timers (RefreshButton.svelte, PositionStrip.svelte, OrderTicket.svelte,
 * SymbolPanel.svelte's Chain-tab resolution), each re-invoking
 * isNseOpen()/isMcxOpen() on its own schedule and caching the result in
 * local component `$state`. One instance of this is wired as the
 * `marketOpenStore` singleton in `stores.js`; kept here (not there) so
 * it's independently unit-testable without a Svelte-compiler-dependent
 * harness — `stores.js` transitively imports a runes (`.svelte.js`)
 * module, which this project's plain-Node Vitest config can't evaluate.
 *
 * `isNseOpen()`/`isMcxOpen()` themselves are unchanged — this only
 * centralizes the "recompute every 30s, plus instantly on a fresh
 * /api/market/status poll" orchestration into one place instead of N.
 *
 * Every field on `deps` is injectable for tests; each defaults to the
 * real browser/global implementation.
 *
 * @param {MarketOpenPollDeps} [deps]
 * @returns {{
 *   read: () => { nseOpen: boolean, mcxOpen: boolean },
 *   start: (onChange: (state: { nseOpen: boolean, mcxOpen: boolean }) => void) => () => void,
 * }}
 */
export function createMarketOpenPoll(deps = {}) {
  const _setInterval = deps.setIntervalFn ?? setInterval;
  const _clearInterval = deps.clearIntervalFn ?? clearInterval;
  const intervalMs = deps.intervalMs ?? 30_000;
  const _onStatusChange = deps.onStatusChange ?? onMarketStatusChange;

  function read() {
    return { nseOpen: isNseOpen(), mcxOpen: isMcxOpen() };
  }

  /**
   * Starts the shared timer + status-change subscription. `onChange`
   * fires immediately with the current reading (so a fresh subscriber
   * never renders a stale seed value), then on every interval tick and
   * every landed /api/market/status poll. Returns a teardown function.
   *
   * @param {(state: { nseOpen: boolean, mcxOpen: boolean }) => void} onChange
   * @returns {() => void}
   */
  function start(onChange) {
    onChange(read());
    const id = _setInterval(() => onChange(read()), intervalMs);
    const unsubStatus = _onStatusChange(() => onChange(read()));
    return () => {
      _clearInterval(id);
      unsubStatus();
    };
  }

  return { read, start };
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
