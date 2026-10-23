// IST-aware date helpers — canonical SSOT for date formatting across all pages.
// All functions use Asia/Kolkata timezone.

/** @returns {string} Today's date in IST as YYYY-MM-DD */
export function todayIST() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
}

/**
 * Epoch-ms of today's session boundary — 00:00 IST (Asia/Kolkata has no DST,
 * so this is a fixed UTC+05:30 offset). Reuses `todayIST()` as the SSOT for
 * "what date is it right now in IST".
 *
 * @returns {number} epoch-ms of 00:00 IST today
 */
export function startOfTodayIST() {
  return Date.parse(`${todayIST()}T00:00:00+05:30`);
}

/**
 * Today's TRADING-SESSION date in IST — like `todayIST()` but rolls over at
 * 08:00 IST (this app's session boundary — see CLAUDE.md "Market daily
 * window", 08:00–23:31 IST, and the backend's own 08:00 IST session-anchor
 * convention in positions.py's `_SESSION_ANCHOR_CUTOFF_TS_SQL`) instead of
 * midnight.
 *
 * Anything before 08:00 IST still returns YESTERDAY's date — the overnight
 * closed window (00:00–08:00) belongs to the trading day that just ended,
 * not a new one. Use this (not `todayIST()`) for any "has the NEXT trading
 * day actually begun" decision — e.g. whether an expired F&O contract has
 * rolled past its last live-spot window. A bare midnight rollover fires
 * hours before the next session opens (~09:15 IST) and several hours
 * before market close even finishes for some segments, which is too early
 * for that kind of decision.
 *
 * @param {number} [nowMs] epoch-ms to evaluate (defaults to `Date.now()`) —
 *   accepting this lets callers ask "what trading-session-date does THIS
 *   timestamp belong to", not just "today's", so the same 08:00 IST
 *   rollover rule can classify an arbitrary row timestamp (see
 *   `isCurrentTradingSession` below).
 * @returns {string} YYYY-MM-DD
 */
export function tradingSessionDateIST(nowMs = Date.now()) {
  const shifted = new Date(nowMs - 8 * 60 * 60 * 1000);
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(shifted);
}

/**
 * True when an epoch-ms timestamp falls in the CURRENT trading session —
 * same trading-session-date as `tradingSessionDateIST()`'s 08:00 IST
 * rollover convention (this app's session boundary; see CLAUDE.md "Market
 * daily window" and `positions.py`'s `_SESSION_ANCHOR_CUTOFF_TS_SQL`).
 * Used to drop stale prior-session rows from live views (e.g. Order Book)
 * without re-deriving the boundary per-caller.
 *
 * @param {number} ms epoch-ms
 * @returns {boolean} false for unparseable/NaN input — caller decides
 *   whether "can't tell" should mean keep or drop.
 */
export function isCurrentTradingSession(ms) {
  if (!Number.isFinite(ms)) return false;
  return tradingSessionDateIST(ms) === tradingSessionDateIST();
}

/**
 * First WEEKDAY (Mon–Fri, IST) strictly AFTER `dateIso` (YYYY-MM-DD),
 * returned as YYYY-MM-DD. Used by `isMcxFutureStillCurrent` below to
 * approximate the backend's holiday-aware `next_market_open_ist` without
 * a client-side holiday calendar (see that function's docstring for the
 * known residual gap this leaves).
 *
 * @param {string} dateIso
 * @returns {string}
 */
function _firstWeekdayIsoAfter(dateIso) {
  let d = new Date(`${dateIso}T00:00:00+05:30`);
  const dateFmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit',
  });
  const wdFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Kolkata', weekday: 'short' });
  // Bounded loop (7 iterations always finds a weekday) — defensive against
  // an unexpected Intl/Date edge case rather than looping forever.
  for (let i = 0; i < 7; i++) {
    d = new Date(d.getTime() + 24 * 60 * 60 * 1000);
    const wd = wdFmt.format(d);
    if (wd !== 'Sat' && wd !== 'Sun') return dateFmt.format(d);
  }
  return dateFmt.format(d);
}

/**
 * MCX-only: true when a future whose REAL listed expiry is `expiryIso`
 * (YYYY-MM-DD, straight from the instruments cache's own `x` field) should
 * still be treated as the live/front-month contract right now.
 *
 * Mirrors the backend's `is_mcx_future_still_current`
 * (`backend/api/algo/expiry_freeze.py`, 2026-10-16 incident fix): a
 * contract stays current through its own ENTIRE expiry day and the whole
 * closed overnight window (23:31→08:00 IST), rolling only once the next
 * trading day's 08:00 IST open has passed — not at bare midnight of the
 * expiry date, which is what `findNearestFuture`/`seedRootMapFromInstruments`
 * used to do (2026-10-23 fix, frontend/backend front-month disagreement).
 *
 * Weekend-aware: a Friday expiry stays current through Saturday and Sunday,
 * rolling only at the following Monday's 08:00 IST (via
 * `_firstWeekdayIsoAfter`), not at Saturday 08:00 — matching the backend's
 * `next_market_open_ist` weekend-skip for the common (non-holiday) case.
 *
 * KNOWN GAP: this module has no client-side holiday calendar (unlike the
 * backend's `next_market_open_ist`, which also consults `market_holidays` +
 * `market_special_sessions`) — a holiday landing immediately after expiry
 * (e.g. a Thursday-expiry Friday-holiday) still rolls at that holiday's own
 * 08:00 instead of waiting for the next REAL trading day. Accepted residual
 * — see CLAUDE.md's "Frontend/backend disagree on front-month MCX contract"
 * entry. Weekends are the overwhelming common case and ARE handled exactly.
 *
 * MCX-only by design, matching the backend ("virtual root anyway will not
 * app[l]y to non-mcx" — operator). Callers for other exchanges (NFO, CDS)
 * must keep their own plain `expiry > today` compare unchanged.
 *
 * @param {string|null|undefined} expiryIso
 * @param {number} [nowMs] epoch-ms to evaluate (defaults to `Date.now()`).
 * @returns {boolean}
 */
export function isMcxFutureStillCurrent(expiryIso, nowMs = Date.now()) {
  if (!expiryIso) return false;
  const sessionToday = tradingSessionDateIST(nowMs);
  if (expiryIso >= sessionToday) return true;
  const boundaryIso = _firstWeekdayIsoAfter(expiryIso);
  return sessionToday < boundaryIso;
}

/** @param {Date|string|number} d @returns {string} e.g. "21 Jul" */
export function formatDateShort(d) {
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit', month: 'short',
  }).format(new Date(d));
}

/** @param {Date|string|number} d @param {Intl.DateTimeFormatOptions} [opts] @returns {string} */
export function formatDateIST(d, opts = {}) {
  return new Intl.DateTimeFormat('en-IN', { timeZone: 'Asia/Kolkata', ...opts }).format(new Date(d));
}
