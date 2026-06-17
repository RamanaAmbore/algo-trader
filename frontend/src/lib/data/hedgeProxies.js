// Hedge-proxy pair table for /admin/derivatives. Pair-only — every
// other parameter (conversion factor, lot count, effective qty) is
// derived at runtime from current LTPs + the instruments cache.
//
// Operator: "to start with table can have goldm and gold, with
// goldbees cross reference, similarly silverm, silver and silverbees.
// the conversion is dynamic, the code should find it based units and
// market value and convert into option lots and qty."
//
// Runtime math (no operator-tunable knobs):
//   factor          = proxy_LTP / target_spot
//   effective_qty   = raw_qty × factor                    (in target units)
//   effective_cost  = raw_cost / factor                    (₹ per target unit)
//   target_lots     = effective_qty / target_lot_size      (from instruments cache)
//   payoff_add(S)   = (S − effective_cost) × effective_qty
//   Δ_extra         = effective_qty

import { fetchHedgeProxies } from '$lib/api';

/** @typedef {{ id:number, proxy_symbol:string, target_root:string,
 *              is_active:boolean, note:string|null }} HedgeProxyRow */

/** @type {HedgeProxyRow[]} */
let _rows = [];
let _loading = null;        // in-flight fetch promise — shared across concurrent callers
let _loadedOnce = false;

/** @type {Record<string, string[]>} */    // target → proxy symbols
let _byTarget = {};
/** @type {Record<string, string[]>} */    // proxy → target roots
let _byProxy = {};

function _rebuildIndices() {
  _byTarget = {};
  _byProxy = {};
  for (const r of _rows) {
    if (!r.is_active) continue;
    const t = String(r.target_root || '').toUpperCase();
    const p = String(r.proxy_symbol || '').toUpperCase();
    if (!t || !p) continue;
    if (!_byTarget[t]) _byTarget[t] = [];
    _byTarget[t].push(p);
    if (!_byProxy[p]) _byProxy[p] = [];
    _byProxy[p].push(t);
  }
}

/**
 * Load the proxy rows from the API. Idempotent; returns the cached
 * value on subsequent calls. `force=true` re-fetches (used by the
 * admin panel after mutations).
 * @param {boolean} [force]
 */
export async function loadHedgeProxies(force = false) {
  if (!force && _loadedOnce) return _rows;
  if (_loading) return _loading;
  _loading = (async () => {
    try {
      const resp = await fetchHedgeProxies();
      _rows = Array.isArray(resp?.rows) ? resp.rows : [];
      _rebuildIndices();
      _loadedOnce = true;
    } catch (_) {
      _rows = [];
      _rebuildIndices();
    } finally {
      _loading = null;
    }
    return _rows;
  })();
  return _loading;
}

/** Cached rows (possibly empty before first load resolves). */
export function getHedgeProxies() { return _rows; }

/**
 * Return the proxy symbols that hedge the given target.
 * @param {string} targetRoot
 * @returns {string[]}
 */
export function proxiesForTarget(targetRoot) {
  return _byTarget[String(targetRoot || '').toUpperCase()] || [];
}

/**
 * Return the target roots the given proxy can hedge.
 * @param {string} proxySymbol
 * @returns {string[]}
 */
export function targetsForProxy(proxySymbol) {
  return _byProxy[String(proxySymbol || '').toUpperCase()] || [];
}

/**
 * Is `proxySymbol` configured to hedge `targetRoot`?
 * @param {string} proxySymbol
 * @param {string} targetRoot
 */
export function isProxyFor(proxySymbol, targetRoot) {
  const arr = _byProxy[String(proxySymbol || '').toUpperCase()];
  if (!arr) return false;
  return arr.includes(String(targetRoot || '').toUpperCase());
}

// Factor helper retired — callers now derive `effective_qty` directly
// as `market_value / target_spot` so the conversion reads as a single
// step in line with the operator's mental model ("number of units ×
// market value ÷ current spot price"). See _mergedPayoff /
// _mergedGreeks in /admin/derivatives.
