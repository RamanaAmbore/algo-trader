/**
 * Pure helpers for updating the _underlyingQuotes map on the derivatives page.
 * Kept separate so they can be unit-tested without importing the Svelte component.
 */

/**
 * Returns a new quotes map with `root`'s ltp updated to `ltp`.
 * No-ops if root not in quotes or ltp is not a finite positive number.
 * @param {Record<string, {ltp: number, day_pct: number|null, prev_close: number}>} quotes
 * @param {string} root
 * @param {number|null|undefined} ltp
 * @returns {Record<string, {ltp: number, day_pct: number|null, prev_close: number}>}
 */
export function applyUnderlyingTickLtp(quotes, root, ltp) {
  if (!(root in quotes)) return quotes;
  const v = Number(ltp);
  if (!Number.isFinite(v) || v <= 0) return quotes;
  if (quotes[root].ltp === v) return quotes;  // same LTP — no $state write needed
  return { ...quotes, [root]: { ...quotes[root], ltp: v } };
}
