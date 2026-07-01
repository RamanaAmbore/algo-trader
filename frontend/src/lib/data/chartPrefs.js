/**
 * chartPrefs — lightweight localStorage persistence for user-selectable
 * chart state (range, series type, overlays, intraday toggle, etc.).
 *
 * Each preference is stored under its own localStorage key.  The module
 * exports a single factory function `usePersistentState(key, default)` that
 * returns a plain object whose `.value` property is a reactive Svelte 5
 * `$state` cell.  Consumers call `.set(v)` to update and persist in one
 * step.
 *
 * SSR safety: localStorage reads happen only on the first `.set()` call
 * triggered from `onMount`, never at module-evaluation time (which runs on
 * the server during SSR).
 *
 * Pattern used for existing series/overlays/signals persistence in
 * ChartWorkspace:
 *   1.  Declare a raw `$state` cell with the hard-coded default.
 *   2.  In `onMount`, read localStorage → write cell → set a hydration flag.
 *   3.  A `$effect` watches the cell; when the flag is true, writes back to
 *       localStorage.
 *
 * This helper encapsulates that 15-line idiom behind a 1-line call:
 *   const range = usePersistentState('rbq.cache.chart-range.v1', 30);
 *   range.value   // reactive read
 *   range.set(90) // update + persist
 *   await range.hydrate(validFn)  // call once from onMount
 *
 * NOTE: This file is a plain JS module (not `.svelte.js`) so it does NOT
 * contain rune declarations at module scope.  Consumers own the `$state`
 * cell; this module only handles the serialisation + hydration contract.
 */

const _LS_AVAILABLE = typeof localStorage !== 'undefined';

/**
 * Low-level localStorage helpers.
 * @param {string} key
 * @returns {any}
 */
function _lsRead(key) {
  if (!_LS_AVAILABLE) return undefined;
  try {
    const raw = localStorage.getItem(key);
    if (raw == null) return undefined;
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

/**
 * @param {string} key
 * @param {any} value
 */
function _lsWrite(key, value) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* quota exceeded — skip silently */
  }
}

/**
 * Read a persisted chart preference from localStorage.
 * Returns `defaultValue` when the key is absent, unreadable, or the
 * stored value fails the optional `validate` check.
 *
 * Call this from `onMount` (never at module-eval time) to stay SSR-safe.
 *
 * @template T
 * @param {string}           key          - localStorage key (e.g. 'rbq.cache.chart-range.v1')
 * @param {T}                defaultValue - returned when key is absent or invalid
 * @param {(v:any) => boolean} [validate] - optional; returns true when the stored value is acceptable
 * @returns {T}
 */
export function readChartPref(key, defaultValue, validate) {
  const stored = _lsRead(key);
  if (stored === undefined || stored === null) return defaultValue;
  if (validate && !validate(stored)) return defaultValue;
  return /** @type {T} */ (stored);
}

/**
 * Write a chart preference to localStorage.
 * Safe to call from any reactive context — does nothing during SSR.
 *
 * @param {string} key
 * @param {any}    value
 */
export function writeChartPref(key, value) {
  _lsWrite(key, value);
}
