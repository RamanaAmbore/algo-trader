// IST-aware date helpers — canonical SSOT for date formatting across all pages.
// All functions use Asia/Kolkata timezone.

/** @returns {string} Today's date in IST as YYYY-MM-DD */
export function todayIST() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
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
