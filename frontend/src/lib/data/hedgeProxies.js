// Hedge-proxy mapping for /admin/derivatives. Stage 2 — the table
// lives in the `hedge_proxies` DB row, edited from /admin/settings
// (no static hard-coding). This module is a thin loader + in-memory
// cache wrapping `fetchHedgeProxies`.
//
// Operator: "somewhere there should be some cross reference between
// root and instrument … don't want to hard code. these tables can
// have multiple columns with parameter values. the conversion can be
// static, dynamic. the correlation can be 0 to 1. for goldbees, and
// silverbees it is one. there could be more parameters. in future, ai
// can generate this info. there should be a panel to enter in the
// current admin settings pages. the code should use this table."
//
// Conversion semantics (driven by row.conversion_kind):
//   - dynamic — factor = proxy_LTP / target_spot   (default for ETFs)
//   - static  — factor = row.static_factor          (fixed at the row)
//   - beta    — factor = row.beta                   (Stage 3, stock vs index)
//
// `correlation` (0..1) scales the effective qty so the operator can
// say "this hedge is 0.85 reliable" and the math reflects that.
//
// Once a factor is in hand the math is the standard linear stock
// formula in target units:
//      effective_qty           = raw_qty   × factor × correlation
//      effective_cost_per_unit = raw_cost  / factor
//      payoff_contribution(S)  = (S − effective_cost) × effective_qty
//      Δ_contribution          = effective_qty

import { fetchHedgeProxies } from '$lib/api';

/** @typedef {{ id:number, proxy_symbol:string, target_root:string,
 *              conversion_kind:'dynamic'|'static'|'beta',
 *              static_factor:number|null, beta:number|null,
 *              correlation:number, kind:'units'|'shares',
 *              note:string|null, source:string, is_active:boolean }} HedgeProxyRow */

/** @type {HedgeProxyRow[]} */
let _rows = [];
let _loading = null;        // in-flight fetch promise so concurrent callers share it
let _loadedOnce = false;

/** @type {Record<string, Array<{ proxy: string, kind: string, row: HedgeProxyRow }>>} */
let _byTarget = {};
/** @type {Record<string, { targets: string[], kind: string, rows: HedgeProxyRow[] }>} */
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
    _byTarget[t].push({ proxy: p, kind: r.kind, row: r });
    if (!_byProxy[p]) _byProxy[p] = { targets: [], kind: r.kind, rows: [] };
    if (!_byProxy[p].targets.includes(t)) _byProxy[p].targets.push(t);
    _byProxy[p].rows.push(r);
  }
}

/**
 * Load the proxy rows from the API. Idempotent — returns the cached
 * value on subsequent calls. Forces a refresh when `force=true`
 * (used by the admin panel after mutations).
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
      // Don't latch — let the next call retry.
    } finally {
      _loading = null;
    }
    return _rows;
  })();
  return _loading;
}

/** Synchronous accessor. Returns whatever's cached now (possibly
 *  empty array on cold start before `loadHedgeProxies()` has resolved). */
export function getHedgeProxies() {
  return _rows;
}

/**
 * Return the list of proxy entries that hedge the given target.
 * Empty array when no proxy is configured.
 * @param {string} targetRoot
 * @returns {Array<{ proxy: string, kind: string, row: HedgeProxyRow }>}
 */
export function proxiesForTarget(targetRoot) {
  return _byTarget[String(targetRoot || '').toUpperCase()] || [];
}

/**
 * Return the list of targets the given proxy can hedge.
 * @param {string} proxySymbol
 * @returns {{ targets: string[], kind: string, rows: HedgeProxyRow[] } | null}
 */
export function proxyTargets(proxySymbol) {
  return _byProxy[String(proxySymbol || '').toUpperCase()] || null;
}

/**
 * Look up the specific (proxy, target) row.
 * @param {string} proxySymbol
 * @param {string} targetRoot
 * @returns {HedgeProxyRow | null}
 */
export function getProxyRow(proxySymbol, targetRoot) {
  const p = String(proxySymbol || '').toUpperCase();
  const t = String(targetRoot || '').toUpperCase();
  const entries = _byTarget[t] || [];
  const hit = entries.find(e => e.proxy === p);
  return hit?.row || null;
}

/**
 * Compute the runtime conversion factor for a given row, given current
 * LTPs. Returns 0 when the factor isn't computable (caller skips the
 * proxy contribution). Operator: "may not 100 % accurate. it will be
 * very close" — bounded by tracking error + bid/ask spread on both
 * sides for dynamic mode; static/beta modes use the row's stored value.
 *
 * @param {HedgeProxyRow} row
 * @param {number} proxyLtp     current LTP of the proxy holding
 * @param {number} targetSpot   current spot of the target underlying
 * @returns {number}
 */
export function computeProxyFactor(row, proxyLtp, targetSpot) {
  if (!row) return 0;
  if (row.conversion_kind === 'static') {
    const f = Number(row.static_factor) || 0;
    return f > 0 ? f : 0;
  }
  if (row.conversion_kind === 'beta') {
    const b = Number(row.beta) || 0;
    return b > 0 ? b : 0;
  }
  // dynamic — the default.
  const p = Number(proxyLtp) || 0;
  const t = Number(targetSpot) || 0;
  if (p <= 0 || t <= 0) return 0;
  return p / t;
}

/**
 * Compute the effective qty in TARGET units. Multiplies by the row's
 * correlation so operator-configured "this hedge is 0.85 reliable"
 * tunes the downstream Δ + payoff line.
 *
 * @param {HedgeProxyRow} row
 * @param {number} factor          from computeProxyFactor()
 * @param {number} rawQty          broker-reported qty of the proxy holding
 */
export function computeEffectiveQty(row, factor, rawQty) {
  if (!row || factor <= 0) return 0;
  const q = Number(rawQty) || 0;
  const c = Number(row.correlation);
  const corr = Number.isFinite(c) && c >= 0 && c <= 1 ? c : 1.0;
  return q * factor * corr;
}
