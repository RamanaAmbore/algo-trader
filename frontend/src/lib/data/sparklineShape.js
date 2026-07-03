/**
 * sparklineShape — dev-only runtime type assertions for the sparkline
 * response payload flowing out of fetchSparklines() into sparklinesStore.
 *
 * Why: the sparkline pipe crosses three tiers (mem cache, DB store,
 * broker), a bare-vs-resolved dual-write, and a per-symbol LTP overlay.
 * Every layer has a documented empty-fallback branch; when the backend
 * ships a data shape that the frontend renderer doesn't expect (bare
 * array vs { symbol -> array } object, wrong number type, NaN
 * poisoning) the cell just renders "—" or a flat line — with no clue
 * where the shape drifted.
 *
 * Gate: DEV-only via `import.meta.env.DEV`; Vite dead-code eliminates
 * the assertion body in the production bundle so operator's browser
 * pays zero cost.
 *
 * Contract (matches backend/api/routes/quote.py:SparklineResponse):
 *   response.data          : { [symbol]: number[] } — >= 2 points each
 *   response.refreshed_at  : ISO-8601 UTC string
 *   response.as_of         : ISO-8601 UTC string | null
 *
 * The per-symbol series contract (matches compose_sparkline_series
 * output when non-empty):
 *   - Array of finite numbers.
 *   - Length >= 2 (renderer needs 2 points to draw a polyline).
 *   - No NaN / Infinity — poison the LTP-tail arithmetic downstream.
 */

const _isDev = (typeof import.meta !== 'undefined')
  && import.meta.env
  && import.meta.env.DEV;

/**
 * Assert the shape of a fetchSparklines() response.
 *
 * @param {any} response  POST /api/quotes/sparkline body
 */
export function assertSparklineResponse(response) {
  if (!_isDev) return;
  if (!response || typeof response !== 'object') {
    throw new Error(`SparklineResponse: not an object (got ${typeof response})`);
  }
  if (!response.data || typeof response.data !== 'object') {
    throw new Error(`SparklineResponse: data missing or non-object (got ${typeof response.data})`);
  }
  if (Array.isArray(response.data)) {
    throw new Error(`SparklineResponse: data is Array — expected { symbol -> number[] } object`);
  }
  if (typeof response.refreshed_at !== 'string') {
    throw new Error(`SparklineResponse: refreshed_at missing or non-string (got ${typeof response.refreshed_at})`);
  }
  // Sample-check the first 3 series so dev-mode overhead stays bounded
  // even on a 100-symbol response.
  const entries = Object.entries(response.data);
  const n = Math.min(entries.length, 3);
  for (let i = 0; i < n; i++) {
    const [sym, series] = entries[i];
    assertSparklineSeries(series, sym);
  }
}

/**
 * Assert a single sparkline series array.
 *
 * @param {any} series  response.data[symbol]
 * @param {string} [sym] optional symbol name for error context
 */
export function assertSparklineSeries(series, sym = '?') {
  if (!_isDev) return;
  if (!Array.isArray(series)) {
    throw new Error(`SparklineSeries[${sym}]: not an array (got ${typeof series})`);
  }
  // Empty is allowed — the backend documents [] as the truly-empty case.
  // But if non-empty, every element must be a finite number.
  if (series.length > 0 && series.length < 2) {
    // The backend pads single-point to [ltp, ltp]; a 1-element series
    // means the pad path drifted. Flag as a shape defect.
    throw new Error(`SparklineSeries[${sym}]: length=1 — expected >=2 (backend pad drift)`);
  }
  for (let i = 0; i < series.length; i++) {
    const v = series[i];
    if (typeof v !== 'number' || !Number.isFinite(v)) {
      throw new Error(`SparklineSeries[${sym}][${i}]: not a finite number (got ${v})`);
    }
  }
}
