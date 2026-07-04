/**
 * displaySymbol — render-layer transform for virtual root symbols.
 *
 * Internal key:  GOLDM_NEXT   (stable machine identifier — used in
 *                               symbolStore keys, API bodies, rootOf()).
 * Display label: GOLDM.NEXT   (dot separator, no spaces — operator spec
 *                               2026-07-03).
 *
 * Only the `_NEXT` suffix is transformed; all other symbols pass through
 * unchanged.  Never call this on internal keys — only at render time.
 *
 * @param {string | null | undefined} virtual  Symbol string to display.
 * @returns {string}
 */
export function displaySymbol(virtual) {
  if (typeof virtual !== 'string') return virtual ?? '';
  return virtual.replace(/_NEXT$/, '.NEXT');
}
