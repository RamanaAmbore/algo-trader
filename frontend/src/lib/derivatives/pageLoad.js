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

import { baseDayPnlForPosition, currentTotalProfit } from '$lib/data/nav.js';
import { todayIST } from '$lib/dateFormat.js';

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

/**
 * Map a raw broker position row into the internal position shape.
 * Does NOT split closed-reopened rows — callers pass the result through
 * splitClosedReopened if needed.
 *
 * @param {any} p  - raw broker position object
 * @param {'live'|'sim'} source
 * @returns {object}
 */
export function buildPositionRowFromBroker(p, source) {
  const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
  return {
    symbol:   sym,
    account:  String(p?.account || ''),
    qty:      Number(p?.quantity || 0),
    // lots / lot_size — new backend fields (quantity is now always contracts).
    // lots = integer lot count for display; lot_size = contracts per lot.
    // Preserved as-is (null when absent) so lotsForRow can use the fast path.
    lots:     p?.lots     != null ? Number(p.lots)     : null,
    lot_size: p?.lot_size != null ? Number(p.lot_size) : null,
    source,
    avg_cost: p?.average_price != null ? Number(p.average_price) : null,
    ltp:      p?.last_price    != null ? Number(p.last_price)    : null,
    prev_close: Number(p?.prev_close) || null,
    pnl:      p?.pnl != null ? Number(p.pnl) : 0,
    realised: p?.realised != null ? Number(p.realised) : 0,
    // unrealised is left undefined (not defaulted to 0) when the backend
    // doesn't ship it — currentTotalProfit()/baseDayPnlForPosition() in
    // nav.js require BOTH realised and unrealised to be present+finite
    // before preferring realised+unrealised over the pnl fallback; a
    // premature 0 default here would silently switch that formula on.
    unrealised: p?.unrealised != null ? Number(p.unrealised) : undefined,
    day_change_val: p?.day_change_val != null ? Number(p.day_change_val) : 0,
    day_pnl: p?.day_pnl != null ? Number(p.day_pnl) : null,
    chg_pct: p?.day_change_percentage != null ? Number(p.day_change_percentage) : null,
    overnight_quantity: Number(p?.overnight_quantity || 0),
    day_buy_quantity:   Number(p?.day_buy_quantity || 0),
    day_sell_quantity:  Number(p?.day_sell_quantity || 0),
    day_buy_value:      Number(p?.day_buy_value || 0),
    day_sell_value:     Number(p?.day_sell_value || 0),
    prev_settlement_pnl: p?.prev_settlement_pnl != null ? Number(p.prev_settlement_pnl) : null,
  };
}

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
// splitClosedReopened — moved from +page.svelte
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Split a broker-consolidated position into separate display rows
 * when it had intraday close/reopen activity.
 *
 * Trigger (Variant 1, with partial-reduction): `overnight ≠ 0` AND
 * (`day_buy > 0` OR `day_sell > 0`).
 *
 * The split produces:
 *   - Closed row  — qty = 0, P&L = realised on the closed portion.
 *   - Open row    — qty = current_qty, P&L = unrealised on what remains.
 *
 * Sum of the two rows' Day P&L equals the original total day change.
 *
 * @param {any} p  - normalised position row (buildPositionRowFromBroker output)
 * @returns {any[]}
 */
/**
 * Compute the weighted average exit price for the closed portion.
 * Long positions exit via sells; short positions exit via buys.
 * @param {number} oq   overnight_quantity
 * @param {number} dbq  day_buy_quantity
 * @param {number} dsq  day_sell_quantity
 * @param {number} dbv  day_buy_value
 * @param {number} dsv  day_sell_value
 * @returns {number}
 */
function _exitPrice(oq, dbq, dsq, dbv, dsv) {
  if (oq > 0) return dsq > 0 ? dsv / dsq : 0;
  return dbq > 0 ? dbv / dbq : 0;
}

/**
 * Day P&L on the closed portion.
 * Long: (exit − prev_close) × closedQty. Short: (prev_close − exit) × closedQty.
 * @param {number} oq          overnight_quantity
 * @param {number} exitPrice
 * @param {number} close       prev_close
 * @param {number} closedQty
 * @returns {number}
 */
function _closedDayPnl(oq, exitPrice, close, closedQty) {
  return oq > 0
    ? (exitPrice - close) * closedQty
    : (close - exitPrice) * closedQty;
}

/**
 * Lifetime P&L attributable to the closed portion.
 * When broker already closed the whole position (brokerQty=0) use p.pnl;
 * otherwise compute from cost basis. Arithmetic is identical to original.
 * @param {number} brokerQty   Math.abs(p.qty)
 * @param {number} pnl         p.pnl
 * @param {number} oq          overnight_quantity
 * @param {number} exitPrice
 * @param {number} avgCost     p.avg_cost
 * @param {number} closedQty
 * @returns {number}
 */
function _closedLifetimePnl(brokerQty, pnl, oq, exitPrice, avgCost, closedQty) {
  if (brokerQty === 0) return pnl;
  return oq > 0
    ? (exitPrice - avgCost) * closedQty
    : (avgCost - exitPrice) * closedQty;
}

/**
 * Force `row.prev_settlement_pnl` so that `baseDayPnlForPosition(row) ===
 * targetDayPnl` exactly, regardless of whether `currentTotalProfit(row)`
 * resolves via `realised+unrealised` or the `pnl` fallback. Used for the
 * "open" half of a closed/reopened split, whose Day P&L is computed
 * independently (`open_dcv` / `baseDayPnlForPosition(p) - closed_day_pnl`)
 * and must not silently diverge depending on which total-profit field pair
 * the backend has populated on the pre-split row.
 * @param {any} row
 * @param {number} targetDayPnl
 * @returns {any} the same row, mutated
 */
function _forceBaseline(row, targetDayPnl) {
  row.prev_settlement_pnl = currentTotalProfit(row) - targetDayPnl;
  return row;
}

/**
 * Entry/exit price + direction for an intraday round-trip (overnight_quantity
 * === 0, both day_buy_quantity and day_sell_quantity > 0 — the position was
 * opened AND partially/fully closed within today's session). Also the path
 * for every Groww row, which hardcodes overnight_quantity=0 regardless of
 * whether the position is actually overnight.
 * @param {number} dbq
 * @param {number} dsq
 * @param {number} dbv
 * @param {number} dsv
 * @returns {{ entry: number, exit: number, closedQty: number, dir: 1|-1 }|null}
 */
function _intradayEntryExit(dbq, dsq, dbv, dsv) {
  const closedQty = Math.min(dbq, dsq);
  if (closedQty <= 0) return null;
  const buyPrice  = dbq > 0 ? dbv / dbq : 0;
  const sellPrice = dsq > 0 ? dsv / dsq : 0;
  // dir=1: net addition was long (bought more than sold) — opened via buys,
  // closed portion exited via sells. dir=-1: net addition was short.
  const dir = /** @type {1|-1} */ (dbq >= dsq ? 1 : -1);
  return {
    entry: dir === 1 ? buyPrice  : sellPrice,
    exit:  dir === 1 ? sellPrice : buyPrice,
    closedQty,
    dir,
  };
}

export function splitClosedReopened(p) {
  const oq  = Number(p.overnight_quantity || 0);
  const dbq = Number(p.day_buy_quantity   || 0);
  const dsq = Number(p.day_sell_quantity  || 0);
  const dbv = Number(p.day_buy_value      || 0);
  const dsv = Number(p.day_sell_value     || 0);
  const close = Number(p.prev_close ?? 0);

  if (dbq === 0 && dsq === 0) return [p];

  if (oq === 0) {
    // Intraday round-trip with no overnight carry — opened and (partially
    // or fully) closed today. Covers intraday partial closes AND every
    // Groww row (overnight_quantity hardcoded to 0 by that broker). No
    // prior-session close exists, so the closed portion's day P&L IS its
    // lifetime P&L (mirrors the "new position" Case-1 convention).
    const trip = _intradayEntryExit(dbq, dsq, dbv, dsv);
    if (!trip) return [p];
    const { entry, exit, closedQty, dir } = trip;
    const closed_lifetime_pnl = dir === 1
      ? (exit - entry) * closedQty
      : (entry - exit) * closedQty;

    const brokerQty = Math.abs(Number(p.qty || 0));
    if (brokerQty === 0) {
      // Fully closed today. Do NOT size the closed row on closedQty
      // (min(dbq,dsq)) alone — Groww hardcodes overnight_quantity=0 even
      // when a position genuinely carried overnight, so a position that
      // closed BOTH a hidden overnight portion AND an intraday round-trip
      // would have the overnight portion's P&L silently dropped if we only
      // counted the round-trip-sized closedQty (min(dbq,dsq) undersizes the
      // true realized amount whenever dbq !== dsq). `currentTotalProfit(p)`
      // (realised+unrealised when both present, else the pnl fallback — the
      // same SSOT baseDayPnlForPosition itself builds on) is the
      // authoritative total realized P&L for the whole (now-flat) position
      // — safe to use directly regardless of Groww's oq mislabeling, and
      // correct even when Groww ships realised+unrealised without a
      // top-level `pnl` field. `baseDayPnlForPosition(p)` on the ORIGINAL
      // unsplit row likewise already resolves Day P&L correctly against
      // whatever prev_settlement_pnl the backend supplied for this
      // (account,symbol) — the backend baseline join is keyed by
      // account+symbol, not by Groww's (untrustworthy) overnight_quantity
      // flag — so it correctly captures any real overnight carry that oq=0
      // hides.
      const wholeLifetimePnl = currentTotalProfit(p);
      const wholeDayPnl = baseDayPnlForPosition(p);
      return [_forceBaseline({
        ...p,
        qty: 0,
        pnl: wholeLifetimePnl,
        realised: wholeLifetimePnl,
        unrealised: 0,
        day_change_val: wholeDayPnl,
        _splitTag: 'closed',
      }, wholeDayPnl)];
    }
    const closedRow = {
      ...p,
      qty: 0,
      pnl: closed_lifetime_pnl,
      // realised/unrealised forced consistent with pnl on the closed row so
      // currentTotalProfit()/baseDayPnlForPosition() agree regardless of
      // which field pair the backend has populated (realised+unrealised vs
      // pnl-only) — otherwise the whole-position realised/unrealised
      // inherited via the spread above (attributable to the FULL position,
      // not just today's closed portion) would double-count now that the
      // backend reliably populates `unrealised` on every row.
      realised: closed_lifetime_pnl,
      unrealised: 0,
      // No prior-session baseline for the closed portion — it was opened
      // AND closed today, so Day P&L IS its lifetime P&L (base=0).
      prev_settlement_pnl: 0,
      day_change_val: closed_lifetime_pnl,
      _splitTag: 'closed',
    };

    const open_dcv_intraday = baseDayPnlForPosition(p) - closed_lifetime_pnl;
    const openRow = _forceBaseline({
      ...p,
      pnl: Number(p.pnl || 0) - closed_lifetime_pnl,
      realised: 0,
      day_change_val: open_dcv_intraday,
      _splitTag: 'open',
    }, open_dcv_intraday);
    return [closedRow, openRow];
  }

  const closed_qty = oq > 0 ? Math.min(oq, dsq) : Math.min(-oq, dbq);
  if (closed_qty <= 0) return [p];

  const exit_price       = _exitPrice(oq, dbq, dsq, dbv, dsv);
  const closed_day_pnl   = _closedDayPnl(oq, exit_price, close, closed_qty);

  const brokerQty        = Math.abs(Number(p.qty || 0));
  const avg_cost         = Number(p.avg_cost || 0);
  const closed_lifetime_pnl = _closedLifetimePnl(
    brokerQty, Number(p.pnl || 0), oq, exit_price, avg_cost, closed_qty
  );

  const open_dcv = baseDayPnlForPosition(p) - closed_day_pnl;

  // Forcing realised/unrealised consistent with pnl (closed portion has no
  // remaining unrealised — qty=0) and forcing prev_settlement_pnl via
  // _forceBaseline means baseDayPnlForPosition(closedRow) = closed_day_pnl
  // regardless of which total-profit field pair the backend has populated.
  const closedRow = _forceBaseline({
    ...p,
    qty: 0,
    pnl: closed_lifetime_pnl,
    realised: closed_lifetime_pnl,
    unrealised: 0,
    day_change_val: closed_day_pnl,
    _splitTag: 'closed',
  }, closed_day_pnl);

  if (brokerQty === 0) return [closedRow];

  const openRow = _forceBaseline({
    ...p,
    pnl: Number(p.pnl || 0) - closed_lifetime_pnl,
    realised: 0,
    day_change_val: open_dcv,
    _splitTag: 'open',
  }, open_dcv);
  return [closedRow, openRow];
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
