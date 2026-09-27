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
 * @returns {string} YYYY-MM-DD
 */
export function tradingSessionDateIST() {
  const shifted = new Date(Date.now() - 8 * 60 * 60 * 1000);
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(shifted);
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
