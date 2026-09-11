/**
 * debugLog — zero-overhead structured logging for RamboQuant frontend.
 *
 * Enable in browser console:
 *   window.__RAMBOQ_DEBUG = 'payoff'   // namespace prefix filter
 *   window.__RAMBOQ_DEBUG = true        // all namespaces
 *   window.__RAMBOQ_DEBUG = false       // off (default)
 *
 * Dump ring buffer:
 *   copy(window.__RAMBOQ_DUMP('payoff'))     // copy JSON to clipboard
 *   window.__RAMBOQ_DOWNLOAD('payoff')       // download as JSON file
 *   window.__RAMBOQ_DOWNLOAD()               // download all
 */

const _ring = /** @type {Array<{ts:number,ns:string,event:string,data:any}>} */ ([]);
const _MAX = 500;

/**
 * Log a structured event. Zero overhead when debug is off.
 * @param {string} ns - namespace (e.g. 'payoff:spot', 'sse', 'navstrip:expiry')
 * @param {string} event - event name
 * @param {any} [data] - optional payload
 */
export function debugLog(ns, event, data) {
  if (!globalThis.__RAMBOQ_DEBUG) return;
  const filter = globalThis.__RAMBOQ_DEBUG;
  if (typeof filter === 'string' && !ns.startsWith(filter)) return;
  const entry = { ts: Date.now(), ns, event, data };
  _ring.push(entry);
  if (_ring.length > _MAX) _ring.shift();
  console.debug(`[RQ:${ns}] ${event}`, data ?? '');
}

if (typeof globalThis !== 'undefined') {
  /**
   * Return ring buffer as JSON string (filtered by namespace prefix if provided).
   * Usage: copy(window.__RAMBOQ_DUMP('payoff'))
   * @param {string} [ns]
   * @returns {string}
   */
  globalThis.__RAMBOQ_DUMP = (ns) => {
    const rows = ns
      ? _ring.filter(e => e.ns === ns || e.ns.startsWith(ns + ':'))
      : _ring;
    return JSON.stringify(rows, null, 2);
  };

  /**
   * Download ring buffer as a JSON file.
   * Usage: window.__RAMBOQ_DOWNLOAD('payoff')
   * @param {string} [ns]
   */
  globalThis.__RAMBOQ_DOWNLOAD = (ns) => {
    const json = globalThis.__RAMBOQ_DUMP(ns);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
    a.download = `ramboq-debug-${ns || 'all'}-${Date.now()}.json`;
    a.click();
  };
}
