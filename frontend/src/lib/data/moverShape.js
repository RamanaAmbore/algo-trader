/**
 * moverShape — dev-only runtime type assertions for MoverRow objects
 * flowing out of fetchMovers() into moversStore.
 *
 * Why: backend payload drift has silently broken the Winners / Losers
 * grids more than once (e.g. `_moverGroups` renamed, `change_pct` typo,
 * `previous_close` moved to a nested block). The grid render code
 * doesn't complain — it just reads `undefined` and paints an empty
 * cell. Result: mystery empty grids that take a full log-diving session
 * to root-cause.
 *
 * How: `assertMoverRow(r)` throws with a specific field-attribution
 * message when the object doesn't match the canonical shape. Called
 * from the moversStore fetcher on the first N rows (cap the noise for
 * a big payload).
 *
 * Gate: assertions are DEV-only via `import.meta.env.DEV`. Production
 * bundles strip the check body via Vite dead-code elimination so
 * there's zero runtime cost on operator's browser.
 *
 * Contract (matches backend/api/routes/watchlist.py:MoverRow):
 *   tradingsymbol   : string, non-empty, uppercase
 *   exchange        : string (NSE / MCX / CDS / BFO / NFO)
 *   last_price      : finite number
 *   previous_close  : finite number
 *   change_pct      : finite number
 *   peak_pct        : finite number
 *   sticky          : boolean
 *   _moverGroups    : string[]  (frontend enrichment, added by moversStore.fetcher)
 *   _moverDirection : 'winners' | 'losers'
 */

const _isDev = (typeof import.meta !== 'undefined')
  && import.meta.env
  && import.meta.env.DEV;

/**
 * Assert a MoverRow-shaped object. Throws Error with a specific
 * field-attribution message when the shape drifts.
 *
 * @param {any} r  candidate row
 * @param {number} [idx] optional row index for error context
 */
export function assertMoverRow(r, idx = -1) {
  if (!_isDev) return;
  const at = idx >= 0 ? ` (row ${idx})` : '';
  if (!r || typeof r !== 'object') {
    throw new Error(`MoverRow${at}: not an object (got ${typeof r})`);
  }
  if (typeof r.tradingsymbol !== 'string' || !r.tradingsymbol) {
    throw new Error(`MoverRow${at}: tradingsymbol missing or non-string (got ${typeof r.tradingsymbol})`);
  }
  if (typeof r.exchange !== 'string' || !r.exchange) {
    throw new Error(`MoverRow${at} ${r.tradingsymbol}: exchange missing or non-string`);
  }
  const numFields = ['last_price', 'change_pct', 'peak_pct'];
  for (const f of numFields) {
    if (typeof r[f] !== 'number' || !Number.isFinite(r[f])) {
      throw new Error(`MoverRow${at} ${r.tradingsymbol}: ${f} not a finite number (got ${r[f]})`);
    }
  }
  // previous_close is nullable when the backend served a snapshot pre-dating
  // that column addition; treat null as OK but non-number as invalid.
  if (r.previous_close !== null && (typeof r.previous_close !== 'number' || !Number.isFinite(r.previous_close))) {
    throw new Error(`MoverRow${at} ${r.tradingsymbol}: previous_close not finite (got ${r.previous_close})`);
  }
  if (typeof r.sticky !== 'boolean') {
    throw new Error(`MoverRow${at} ${r.tradingsymbol}: sticky not boolean (got ${typeof r.sticky})`);
  }
  // Frontend enrichment fields — added by moversStore.fetcher.
  if (r._moverGroups !== undefined && !Array.isArray(r._moverGroups)) {
    throw new Error(`MoverRow${at} ${r.tradingsymbol}: _moverGroups not array (got ${typeof r._moverGroups})`);
  }
  if (r._moverDirection !== undefined
      && r._moverDirection !== 'winners' && r._moverDirection !== 'losers') {
    throw new Error(`MoverRow${at} ${r.tradingsymbol}: _moverDirection must be 'winners'|'losers' (got ${r._moverDirection})`);
  }
}

/**
 * Batch-assert a MoverRow[] array. Caps assertion to the first
 * `maxRows` entries to keep dev-mode reactive cost bounded on a
 * calm day when 40 rows land.
 *
 * @param {any[]} rows
 * @param {number} [maxRows=5]
 */
export function assertMoverRows(rows, maxRows = 5) {
  if (!_isDev) return;
  if (!Array.isArray(rows)) {
    throw new Error(`MoverRows: not an array (got ${typeof rows})`);
  }
  const n = Math.min(rows.length, maxRows);
  for (let i = 0; i < n; i++) {
    assertMoverRow(rows[i], i);
  }
}
