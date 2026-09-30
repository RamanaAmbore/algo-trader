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
