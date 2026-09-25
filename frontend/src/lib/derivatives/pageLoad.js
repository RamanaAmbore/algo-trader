/**
 * pageLoad.js — Pure data-transform helpers for the /admin/derivatives page.
 *
 * All functions are pure (no Svelte reactive state, no DOM access).
 * The async functions (loadPositions / loadStrategy) in +page.svelte call
 * these with plain values extracted from reactive state, keeping cc low in
 * the .svelte while keeping logic testable.
 *
 * Extracted from frontend/src/routes/(algo)/admin/derivatives/+page.svelte
 * to reduce cyclomatic complexity in three hotspots:
 *   loadPositions (cc=76), loadStrategy (cc=50), candidatePositions (cc=43).
 */

import { todayIST } from '$lib/dateFormat.js';

// splitClosedReopened + buildPositionRowFromBroker moved to
// $lib/data/expiryPnl.js (2026-09 SSOT fix) so portfolioStore.svelte.js can
// reuse the same precise partial/full-close realised-P&L derivation without
// a derivatives-page dependency. Re-exported here unchanged so existing
// call sites (+page.svelte, pageLoad.test.js) are unaffected.
export { buildPositionRowFromBroker, splitClosedReopened } from '$lib/data/expiryPnl.js';

// ─────────────────────────────────────────────────────────────────────────────
// Shared predicates
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Returns true when the trading symbol belongs to F&O (options / futures).
 * Cash equities, ETFs etc. return false.
 *
 * @param {string|null|undefined} sym
 * @returns {boolean}
 */
export function isFOSymbol(sym) {
  return /(CE|PE|FUT)$/i.test(String(sym || ''));
}

/**
 * Build an expiry-match predicate.
 * Empty selectedExpiries = all expiries pass (fail-open / no filter).
 *
 * @param {string[]} selectedExpiries  - YYYY-MM-DD list; empty = no filter
 * @param {(sym: string) => {x?: string} | null} getInstrument
 * @returns {(sym: string) => boolean}
 */
export function buildExpiryMatcher(selectedExpiries, getInstrument) {
  if (!selectedExpiries.length) return () => true;
  return (sym) => {
    const inst = getInstrument(String(sym || '').toUpperCase());
    return selectedExpiries.includes(inst?.x);
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Position / holding row builders
// ─────────────────────────────────────────────────────────────────────────────
// buildPositionRowFromBroker moved to $lib/data/expiryPnl.js — re-exported
// above unchanged.

/**
 * Map a raw broker holding row into the internal holdings shape.
 * Returns null when both qty and opening_qty are zero (row should be skipped).
 *
 * @param {any} h  - raw broker holding object
 * @returns {object|null}
 */
export function buildHoldingRowFromBroker(h) {
  const sym = String(h?.tradingsymbol || h?.symbol || '').toUpperCase();
  if (!sym) return null;
  const qty = Number(h?.quantity || 0);
  const openingQty = Number(h?.opening_quantity || 0);
  if (!qty && !openingQty) return null;
  return {
    symbol:     sym,
    account:    String(h?.account || ''),
    qty,
    opening_qty: openingQty,
    avg_cost:   h?.average_price != null ? Number(h.average_price) : null,
    ltp:        h?.last_price    != null ? Number(h.last_price)    : null,
    prev_close: Number(h?.prev_close) || null,
    pnl:        h?.pnl != null ? Number(h.pnl) : 0,
    day_change_val: h?.day_change_val != null ? Number(h.day_change_val) : 0,
  };
}

/**
 * Increment the excluded-account P&L totals map in-place.
 * Equity intraday positions / derivative holdings are excluded from the
 * F&O panel but must still reconcile against the navbar PositionStrip.
 *
 * @param {Record<string, {pos_pnl:number,pos_day:number,hold_pnl:number,hold_day:number}>} excluded
 * @param {string} acct
 * @param {Partial<{pos_pnl:number,pos_day:number,hold_pnl:number,hold_day:number}>} delta
 */
export function bumpExcluded(excluded, acct, delta) {
  const a = String(acct || '').toUpperCase();
  if (!excluded[a]) {
    excluded[a] = { pos_pnl: 0, pos_day: 0, hold_pnl: 0, hold_day: 0 };
  }
  excluded[a].pos_pnl  += Number(delta.pos_pnl  || 0);
  excluded[a].pos_day  += Number(delta.pos_day  || 0);
  excluded[a].hold_pnl += Number(delta.hold_pnl || 0);
  excluded[a].hold_day += Number(delta.hold_day || 0);
}

// ─────────────────────────────────────────────────────────────────────────────
// candidatePositions body
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Derive the candidate position rows for the selected underlying.
 *
 * Mirrors the $derived.by body in +page.svelte so the pure logic is
 * testable without reactive wiring. The $derived shell in the page
 * passes all reactive state as plain values here.
 *
 * Three position sources appear in this order:
 *   1. Real positions (live/sim) + equity holdings + proxy hedges + local drafts
 *   2. Provisional positions (~) — fills received before broker book refreshes
 *   3. Draft store positions (D) — operator-added hypothetical positions from store
 *
 * @param {{
 *   positions: any[],
 *   holdings: any[],
 *   drafts: any[],
 *   target: string,
 *   selectedExpiries: string[],
 *   selectedAccounts: string[],
 *   simActive: boolean,
 *   proxiesForTarget: (t: string) => string[],
 *   getInstrument: (sym: string) => {x?: string} | null,
 *   provisionalPositions?: Map<string, any>,
 *   draftStorePositions?: Map<string, any>,
 * }} params
 * @returns {any[]}
 */
export function buildCandidatePositions({
  positions, holdings, drafts,
  target, selectedExpiries, selectedAccounts, simActive,
  proxiesForTarget, getInstrument,
  provisionalPositions,
  draftStorePositions,
}) {
  const prefixRe = new RegExp(`^${target}\\d`, 'i');
  const wantedSource = simActive ? 'sim' : 'live';
  const matchExpiry  = buildExpiryMatcher(selectedExpiries, getInstrument);

  // buildAcctMatcher from derivativesMath handles empty = fail-open
  // but is not imported here to keep this module self-contained.
  // Inline equivalent (same semantics — account values are always uppercase).
  const matchAccount = selectedAccounts.length === 0
    ? () => true
    : (acct) => selectedAccounts.includes(String(acct || ''));

  /** @type {any[]} */
  const real = [];
  /** @type {any[]} */
  const provisional = [];
  /** @type {any[]} */
  const draftStore = [];

  // F&O positions
  for (const p of positions) {
    if (p.source !== wantedSource) continue;
    if (!matchAccount(p.account)) continue;
    const sym = p.symbol;
    if (!prefixRe.test(sym)) continue;
    const isFut = /FUT$/i.test(sym);
    const isOpt = /(CE|PE)$/i.test(sym);
    if (!isFut && !isOpt) continue;
    if (Number(p?.qty || 0) !== 0 && !matchExpiry(sym)) continue;
    // Skip instruments no longer in master (expired and removed by Kite).
    const _inst = getInstrument(sym);
    if (!_inst && Number(p?.qty || 0) !== 0) continue;
    // Skip contracts where the expiry date has already passed.
    if (_inst?.x && _inst.x < todayIST() && Number(p?.qty || 0) !== 0) continue;
    real.push({ ...p, kind: isFut ? 'fut' : 'opt' });
  }

  // Direct equity holdings of the underlying
  for (const h of holdings) {
    const sym = String(h.symbol || '').toUpperCase();
    if (sym !== target) continue;
    if (!matchAccount(h.account)) continue;
    if (!Number(h.qty || 0)) continue;
    real.push({ ...h, source: 'live', kind: 'eq' });
  }

  // Proxy hedges (GOLDBEES → GOLD etc.)
  const _allowedProxies = new Set(proxiesForTarget(target));
  if (_allowedProxies.size) {
    for (const h of holdings) {
      const sym = String(h.symbol || '').toUpperCase();
      if (!_allowedProxies.has(sym)) continue;
      if (!matchAccount(h.account)) continue;
      if (!Number(h.qty || 0)) continue;
      real.push({ ...h, source: 'live', kind: 'eq', proxy_for: target });
    }
  }

  // Local drafts — no account filter (drafts are not tied to a broker account)
  for (const d of drafts) {
    const sym = String(d.symbol || '').toUpperCase();
    if (!sym || !prefixRe.test(sym)) continue;
    const isFut = /FUT$/i.test(sym);
    const isOpt = /(CE|PE)$/i.test(sym);
    if (!isFut && !isOpt) continue;
    if (!matchExpiry(sym)) continue;
    const _dInst = getInstrument(sym);
    if (!_dInst) continue;
    if (_dInst.x && _dInst.x < todayIST()) continue;
    const qty  = d.qty      === '' || d.qty      == null ? 0    : Number(d.qty);
    const cost = d.avg_cost === '' || d.avg_cost == null ? null : Number(d.avg_cost);
    const ltp  = d.ltp      === '' || d.ltp      == null ? null : Number(d.ltp);
    real.push({
      symbol: sym, account: '', qty, avg_cost: cost, ltp,
      source: 'draft', kind: isFut ? 'fut' : 'opt', draftId: d.id,
    });
  }

  // Provisional positions (~) — post-fill, pre-broker-refresh
  if (provisionalPositions) {
    for (const entry of provisionalPositions.values()) {
      const sym = String(entry.tradingsymbol || '').toUpperCase();
      if (!sym || !prefixRe.test(sym)) continue;
      const isFut = /FUT$/i.test(sym);
      const isOpt = /(CE|PE)$/i.test(sym);
      if (!isFut && !isOpt) continue;
      if (!matchExpiry(sym)) continue;
      if (!matchAccount(entry.account)) continue;
      provisional.push({
        symbol:    sym,
        account:   String(entry.account || ''),
        qty:       Number(entry.quantity || 0),
        lots:      entry.lots     != null ? Number(entry.lots)     : null,
        lot_size:  entry.lot_size != null ? Number(entry.lot_size) : null,
        avg_cost:  entry.average_price != null ? Number(entry.average_price) : null,
        ltp:       entry.last_price    != null ? Number(entry.last_price)    : null,
        prev_close: 0,
        pnl:       Number(entry.pnl || 0),
        realised:  Number(entry.realised || 0),
        day_change_val: Number(entry.day_change_val || 0),
        source:    'provisional',
        kind:      isFut ? 'fut' : 'opt',
        _provisional: true,
      });
    }
  }

  // Draft store positions (D) — operator-added hypothetical store entries
  if (draftStorePositions) {
    for (const entry of draftStorePositions.values()) {
      const sym = String(entry.tradingsymbol || '').toUpperCase();
      if (!sym || !prefixRe.test(sym)) continue;
      const isFut = /FUT$/i.test(sym);
      const isOpt = /(CE|PE)$/i.test(sym);
      if (!isFut && !isOpt) continue;
      if (!matchExpiry(sym)) continue;
      if (!matchAccount(entry.account)) continue;
      draftStore.push({
        symbol:    sym,
        account:   String(entry.account || ''),
        qty:       Number(entry.quantity || 0),
        lots:      entry.lots     != null ? Number(entry.lots)     : null,
        lot_size:  entry.lot_size != null ? Number(entry.lot_size) : null,
        avg_cost:  entry.average_price != null ? Number(entry.average_price) : null,
        ltp:       entry.last_price    != null ? Number(entry.last_price)    : null,
        prev_close: 0,
        pnl:       Number(entry.pnl || 0),
        realised:  Number(entry.realised || 0),
        day_change_val: Number(entry.day_change_val || 0),
        source:    'draft_store',
        kind:      isFut ? 'fut' : 'opt',
        _draft_store: true,
      });
    }
  }

  // Sort each group: closed (qty=0) to end within group, stable otherwise.
  const closedLast = (a, b) => {
    const ac = (Number(a?.qty || 0) === 0) ? 1 : 0;
    const bc = (Number(b?.qty || 0) === 0) ? 1 : 0;
    return ac - bc;
  };
  real.sort(closedLast);
  provisional.sort(closedLast);
  draftStore.sort(closedLast);

  // Ordering: real first, then provisional (~), then draft store (D).
  return [...real, ...provisional, ...draftStore];
}

// ─────────────────────────────────────────────────────────────────────────────
// loadStrategy helpers
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Filter and normalise the legs array for the strategy analytics endpoint.
 * Equity (kind='eq') legs are excluded — the backend only accepts F&O.
 * Ltp is inlined only for sim / draft sources.
 *
 * @param {any[]} legs
 * @param {(sym: string) => {x?: string} | null} getInstrument
 * @returns {{symbol:string, qty:number, avg_cost:number|null, ltp:number|null, expiry:string|null}[]}
 */
export function buildCleanLegs(legs, getInstrument) {
  return legs
    .filter(l => l.kind !== 'eq')
    .map(l => {
      const sym    = String(l.symbol || '').trim().toUpperCase();
      const inst   = sym ? getInstrument(sym) : null;
      const expiry = inst?.x || null;
      return {
        symbol:   sym,
        qty:      l.qty === '' || l.qty == null ? 0 : Number(l.qty),
        avg_cost: l.avg_cost === '' || l.avg_cost == null ? null : Number(l.avg_cost),
        ltp: (l.source === 'sim' || l.source === 'draft' ||
              l.source === 'provisional' || l.source === 'draft_store')
          ? (l.ltp === '' || l.ltp == null ? null : Number(l.ltp))
          : null,
        expiry,
      };
    })
    .filter(l => l.symbol && l.qty && !(l.expiry && l.expiry < todayIST()));
}

/**
 * Build a stable string key from the cleanLegs array for memoisation.
 * Two calls with identical legs produce identical keys.
 *
 * @param {{symbol:string, qty:number, avg_cost:number|null, ltp:number|null, expiry:string|null}[]} cleanLegs
 * @returns {string}
 */
export function computeLegsKey(cleanLegs) {
  return cleanLegs.map(l =>
    `${l.symbol}:${l.qty}:${l.avg_cost ?? ''}:${l.ltp ?? ''}:${l.expiry ?? ''}`
  ).join('|');
}

/**
 * Detect whether the first clean leg's underlying root has changed
 * vs the currently-displayed strategy.
 *
 * @param {{symbol:string}[]} cleanLegs
 * @param {any|null} currentStrategy  - the strategy object currently rendered
 * @param {(sym:string) => {root:string}} decomposeSymbol
 * @returns {boolean}
 */
export function didUnderlyingChange(cleanLegs, currentStrategy, decomposeSymbol) {
  if (!cleanLegs.length) return false;
  const newU = decomposeSymbol(cleanLegs[0].symbol).root;
  const prevLegs = currentStrategy?.legs;
  if (!prevLegs?.length) return false;
  const oldU = decomposeSymbol(prevLegs[0].symbol).root;
  return !!(newU && oldU && newU !== oldU);
}

/**
 * Build a cache key for the equity-only synth strategy.
 * Encodes (underlying, per-leg symbol+qty+cost+ltp) so a re-derive
 * only fires when inputs actually changed.
 *
 * @param {string} underlying  - selectedUnderlying value
 * @param {any[]} eqs          - equity leg rows
 * @returns {string}
 */
export function synthCacheKey(underlying, eqs) {
  const parts = [underlying || ''];
  for (const e of eqs) {
    parts.push(
      `${e.symbol || ''}:${Number(e.qty) || 0}:${Number(e.avg_cost) || 0}:${Number(e.ltp) || 0}`
    );
  }
  return parts.join('|');
}

/**
 * Build a strategy-shaped stub for an equity-only basket so the payoff
 * card renders a linear long-stock curve when no options/futures are present.
 * Returns null when no eq leg has a usable spot anchor.
 *
 * @param {any[]} eqs       - equity leg rows (kind='eq')
 * @param {string} underlying - selectedUnderlying value
 * @returns {object|null}
 */
export function synthEquityOnlyStrategy(eqs, underlying) {
  if (!Array.isArray(eqs) || eqs.length === 0) return null;
  const primary = eqs.find(e => Number(e.ltp) > 0) || eqs[0];
  const spot = Number(primary.ltp) || Number(primary.avg_cost) || 0;
  if (spot <= 0) return null;

  const prevClose = Number(primary.prev_close) || spot;
  const spanPct   = 0.15;
  const N = 41;
  const lo = spot * (1 - spanPct);
  const hi = spot * (1 + spanPct);

  const payoff = [];
  for (let i = 0; i < N; i++) {
    const s = lo + (i / (N - 1)) * (hi - lo);
    payoff.push({ spot: s, today_value: 0, expiry_value: 0 });
  }

  let netCost = 0;
  for (const e of eqs) {
    const qty  = Number(e.qty) || Number(e.opening_qty) || 0;
    const cost = Number(e.avg_cost) || 0;
    netCost += qty * cost;
  }

  return {
    payoff,
    spot,
    spot_prev_close:      prevClose,
    spot_source:          'live',
    spot_anchor_contract: null,
    underlying:           underlying || '',
    legs:                 [],
    multi_expiry:         false,
    expiry:               null,
    days_to_expiry:       0,
    span_sigmas:          0,
    span_pct:             spanPct,
    iv_proxy:             0,
    net_cost:             netCost,
    intermediate_curves:  [],
    risk: {
      max_profit: 0, max_loss: 0, breakevens: [],
      rr_ratio: null, ev: null, ev_pct: null, pop: null,
    },
    aggregate_greeks: { delta: 0, gamma: 0, theta: 0, vega: 0, rho: 0 },
  };
}
