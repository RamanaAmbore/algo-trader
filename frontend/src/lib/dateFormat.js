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
