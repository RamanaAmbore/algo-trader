// Hedge-proxy mapping for the /admin/derivatives Underlying picker +
// Legs panel. Operator: "i have goldbees and silverbees. is there
// anyway to use them based units and qty to hedge against option in
// legs and payoff … this concept should be generalized when there is
// no strictly underlying relation." Stage 1.
//
// Two-step approach:
//   1. A small inline list of `proxy → targets[]` pairings tells the
//      page which holdings hedge which underlyings (GOLDBEES hedges
//      GOLD / GOLDM, NIFTYBEES hedges NIFTY, …). This is identity
//      mapping only — NO conversion factor stored.
//   2. The conversion factor is derived AT RUNTIME from current LTPs:
//          factor = proxy_LTP / target_spot
//      Operator: "is there anyway you determine qty and lot size based
//      current value of total silverbees and goldbees holdings. it may
//      not 100% accurate. it will be very close, no need to have a
//      separate table." So the page reads the live GOLDBEES LTP and
//      the live GOLD front-month spot, divides them, and uses the
//      ratio as the conversion factor. Stays self-calibrating as both
//      prices drift.
//
// Once a factor is in hand the math is the standard linear stock
// formula in target units:
//      effective_qty           = raw_qty   × factor
//      effective_cost_per_unit = raw_cost  / factor
//      payoff_contribution(S)  = (S − effective_cost) × effective_qty
//                              = (S × factor − raw_cost) × raw_qty
//
// Symmetric for delta: Δ_contribution = effective_qty.

/**
 * Identity-only mapping. Each entry tells the derivatives page that
 * the operator's holding in `proxy` can hedge an option position on
 * any of the listed `targets`. The factor is NOT stored here — see
 * the module docstring for the runtime derivation.
 *
 * @typedef {{ proxy: string, targets: string[], kind: 'units'|'shares' }} ProxyPair
 */

/** @type {ProxyPair[]} */
const PROXY_PAIRS = [
  { proxy: 'GOLDBEES',   targets: ['GOLD', 'GOLDM', 'GOLDPETAL', 'GOLDGUINEA'], kind: 'units'  },
  { proxy: 'SILVERBEES', targets: ['SILVER', 'SILVERM', 'SILVERMIC'],           kind: 'units'  },
  { proxy: 'NIFTYBEES',  targets: ['NIFTY'],                                    kind: 'shares' },
  { proxy: 'BANKBEES',   targets: ['BANKNIFTY'],                                kind: 'shares' },
];

/** Reverse index: target → list of proxy symbols that hedge it.
 *  Built once at module load — keys are uppercase target roots, values
 *  are arrays of {proxy, kind} so callers can iterate proxies for a
 *  picked underlying without re-walking PROXY_PAIRS each render. */
const _BY_TARGET = (() => {
  /** @type {Record<string, Array<{ proxy: string, kind: 'units'|'shares' }>>} */
  const m = {};
  for (const p of PROXY_PAIRS) {
    for (const t of p.targets) {
      const k = String(t).toUpperCase();
      if (!m[k]) m[k] = [];
      m[k].push({ proxy: p.proxy, kind: p.kind });
    }
  }
  return m;
})();

/** Reverse index: proxy → list of targets it can hedge. Mirror of
 *  PROXY_PAIRS but keyed for O(1) lookup. */
const _BY_PROXY = (() => {
  /** @type {Record<string, { targets: string[], kind: 'units'|'shares' }>} */
  const m = {};
  for (const p of PROXY_PAIRS) {
    m[String(p.proxy).toUpperCase()] = { targets: p.targets.map(t => String(t).toUpperCase()), kind: p.kind };
  }
  return m;
})();

/**
 * Return the list of proxy symbols that hedge the given target. Empty
 * array when no proxy is configured (no hedge available).
 * @param {string} targetRoot
 * @returns {Array<{ proxy: string, kind: 'units'|'shares' }>}
 */
export function proxiesForTarget(targetRoot) {
  return _BY_TARGET[String(targetRoot || '').toUpperCase()] || [];
}

/**
 * Return the list of targets the given proxy can hedge, plus the
 * conversion kind ('units' for ETFs tracking precious metals, 'shares'
 * for index ETFs). Returns null when the symbol isn't a known proxy.
 * @param {string} proxySymbol
 * @returns {{ targets: string[], kind: 'units'|'shares' } | null}
 */
export function proxyTargets(proxySymbol) {
  return _BY_PROXY[String(proxySymbol || '').toUpperCase()] || null;
}

/**
 * True when `proxySymbol` is a hedge proxy for `targetRoot`.
 * @param {string} proxySymbol
 * @param {string} targetRoot
 */
export function isProxyFor(proxySymbol, targetRoot) {
  const p = _BY_PROXY[String(proxySymbol || '').toUpperCase()];
  if (!p) return false;
  return p.targets.includes(String(targetRoot || '').toUpperCase());
}

/**
 * Compute the runtime conversion factor from current LTPs. Returns 0
 * when either price is missing or non-positive — caller treats 0 as
 * "can't convert, skip the proxy contribution". Operator: "may not
 * 100% accurate. it will be very close." — accuracy is bounded by
 * tracking error + bid/ask spread on both sides.
 * @param {number} proxyLtp     current LTP of the held proxy (e.g. GOLDBEES @ ₹95)
 * @param {number} targetSpot   current spot of the target underlying (e.g. GOLD @ ₹9,500/g)
 * @returns {number}
 */
export function computeProxyFactor(proxyLtp, targetSpot) {
  const p = Number(proxyLtp) || 0;
  const t = Number(targetSpot) || 0;
  if (p <= 0 || t <= 0) return 0;
  return p / t;
}
