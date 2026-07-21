// pulseUnified.js — Pure row-building helpers for the MarketPulse
// buildUnified compositor.
//
// All functions here are side-effect-free and take explicit arguments —
// no Svelte reactive reads, no module-level state. The component passes a
// `ctx` bag containing function references that wrap any reactive reads
// (e.g. snapOf wraps `untrack(() => getSnapshot(sym))`).
//
// Section order mirrors the original buildUnified implementation so diffs
// remain readable:
//   1. mergeWatchlistRows
//   2. mergePositionRows
//   3. mergeHoldingRows
//   4. mergeUnderlyingAnchors
//   5. mergeMoverRows
//   6. tagWatchedIndices
//   7. finalizeRows
//   8. sortUnifiedRows
//
// Shared utilities (also exported for unit tests):
//   parseSymbolFallback, parseSymbol, fillSymbolMeta, makeRowFactory

import { livePositionDayPnl } from './nav.js';

// ── Shared constants ─────────────────────────────────────────────────────────

export const MAJOR_ORDER = { pinned: 0, watchlist: 1, positions: 2, holdings: 3, movers: 4 };

export const MAJOR_SUFFIX = {
  pinned:    '__pin',
  watchlist: '__wl',
  positions: '__pos',
  holdings:  '__hold',
  movers:    '__mov',
};

// Watched-index → underlying mapping. Used by tagWatchedIndices.
export const INDEX_TO_UNDERLYING = {
  'NIFTY 50':           'NIFTY',
  'NIFTY BANK':         'BANKNIFTY',
  'NIFTY FIN SERVICE':  'FINNIFTY',
  'NIFTY IT':           'NIFTYIT',
  'NIFTY MID SELECT':   'MIDCPNIFTY',
  'NIFTY MIDCAP 100':   'MIDCPNIFTY',
  'NIFTY MIDCAP 50':    'MIDCPNIFTY',
  'NIFTY MIDCAP 150':   'MIDCPNIFTY',
  'NIFTY NXT 50':       'NIFTYNXT50',
  'NIFTY SMLCAP 100':   'SMALLCAP',
  'NIFTY SMLCAP 250':   'SMALLCAP',
  'NIFTY SMALLCAP 100': 'SMALLCAP',
  'NIFTY SMALLCAP 50':  'SMALLCAP',
  'SENSEX':             'SENSEX',
  'BANKEX':             'BANKEX',
  'INDIA VIX':          'INDIAVIX',
};

// ── Shared pure utilities ─────────────────────────────────────────────────────

/**
 * Regex-only parser for F&O tradingsymbols when the instruments
 * cache misses (commodity options, new contracts, dev fixtures).
 *
 * @param {string} sym
 * @returns {{ underlying: string|null, kind: string|null, strike: number|null, opt_type: string|null, expiry: string|null }}
 */
export function parseSymbolFallback(sym) {
  const empty = { underlying: null, kind: null, strike: null, opt_type: null, expiry: null };
  if (!sym) return empty;
  const m = /^([A-Z][A-Z&]+?)(\d{2}[A-Z]{3})(\d+)?(CE|PE|FUT)$/.exec(sym);
  if (!m) return empty;
  const [, underlying, expiry, strikeStr, suffix] = m;
  const strike = strikeStr ? Number(strikeStr) : null;
  const opt_type = (suffix === 'CE' || suffix === 'PE') ? suffix : null;
  const kind = opt_type ? 'opt' : (suffix === 'FUT' ? 'fut' : null);
  return { underlying, kind, strike, opt_type, expiry };
}

/**
 * Parse a tradingsymbol using the instruments cache (`getInst`) where
 * available, falling back to the regex parser.
 *
 * @param {string} sym
 * @param {((s: string) => any) | null} getInst
 * @returns {{ underlying: string|null, kind: string|null, strike: number|null, opt_type: string|null, expiry: string|null }}
 */
export function parseSymbol(sym, getInst) {
  const inst = getInst ? getInst(sym) : null;
  if (inst) {
    const t = String(inst.t || '').toUpperCase();
    const k = inst.k != null ? Number(inst.k) : null;
    let optType = null;
    if (t === 'CE' || t === 'PE') optType = t;
    else if (/CE$/i.test(sym)) optType = 'CE';
    else if (/PE$/i.test(sym)) optType = 'PE';
    const kind = optType ? 'opt' : (t === 'FUT' ? 'fut' : (t === 'EQ' ? 'eq' : null));
    return { underlying: inst.u || null, kind, strike: k, opt_type: optType, expiry: inst.x || null };
  }
  return parseSymbolFallback(sym);
}

/**
 * Write symbol-metadata fields (underlying, kind, strike, opt_type, expiry)
 * onto `row` only when the field is still null (first-write wins).
 *
 * @param {Record<string, any>} row
 * @param {string} sym
 * @param {((s: string) => any) | null} getInst
 */
export function fillSymbolMeta(row, sym, getInst) {
  const p = parseSymbol(sym, getInst);
  if (row.underlying == null) row.underlying = p.underlying;
  if (row.kind       == null) row.kind       = p.kind;
  if (row.strike     == null) row.strike     = p.strike;
  if (row.opt_type   == null) row.opt_type   = p.opt_type;
  if (row.expiry     == null) row.expiry     = p.expiry;
}

/**
 * Create the `get(sym, major)` row-factory closure for a `byKey` map.
 *
 * @param {Record<string, any>} byKey
 * @returns {(sym: string, major: string) => Record<string, any>}
 */
export function makeRowFactory(byKey) {
  return function get(sym, major) {
    const key = `${sym}${MAJOR_SUFFIX[major]}`;
    if (byKey[key]) return byKey[key];
    return byKey[key] = {
      key,
      _majorGroup: major,
      _majorOrder: MAJOR_ORDER[major],
      src: { w: false, h: false, p: false, u: false, m: false },
      qty_pos: 0, qty_hold: 0,
      pnl: null, day_pnl: null,
      _avg_num: 0,
      _avg_hold_num: 0,
      accounts: new Set(),
    };
  };
}

// ── Internal micro-helpers (reduce duplicated branches across sections) ──────

/**
 * Copy account-level stale flags + price_source / is_animating propagation
 * from a raw broker row onto the aggregated pulse row.
 *
 * This block appears verbatim in mergePositionRows + mergeHoldingRows so it
 * lives in one place. Written for behaviour parity — no logic changes.
 *
 * @param {Record<string, any>} row  target aggregated pulse row (mutated)
 * @param {Record<string, any>} r    raw broker input row
 */
function _propagateStaleAndSource(row, r) {
  if (r.account_stale === true) {
    row.account_stale = true;
    if (r.account_stale_since) row.account_stale_since = r.account_stale_since;
  }
  const _rps = r.price_source ?? r.ltp_source;
  if (_rps && _rps !== 'live') {
    row.price_source = row.price_source && row.price_source !== 'live'
      ? row.price_source : _rps;
  }
  if (r.is_animating === false) row.is_animating = false;
}

/**
 * Collect the set of UPPER underlyings for every CE/PE row in `rows`.
 * Empty when `rows` is empty or all rows are non-option.
 *
 * Extracted from mergeUnderlyingAnchors where the same walk was inlined
 * twice (once for positions, once for holdings).
 *
 * @param {any[]} rows              broker rows to scan
 * @param {((s: string) => any) | null} getInst  instruments cache accessor
 * @returns {Set<string>}
 */
function _optUnderlyingSet(rows, getInst) {
  const out = new Set();
  for (const r of rows) {
    const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
    if (!/(CE|PE)$/i.test(sym)) continue;
    const p = parseSymbol(sym, getInst);
    if (p?.underlying) out.add(String(p.underlying).toUpperCase());
  }
  return out;
}

/**
 * Apply quote fields from snap/liveQ priority chain onto a row object.
 * Returns true if ltp was written from snap or liveQ; false if neither
 * source had an ltp value and the caller must handle its own fallback.
 *
 * Module-private — not exported.
 *
 * @param {Record<string, any>} row   target aggregated pulse row (mutated)
 * @param {any}  snap                 symbolStore snapshot (may be null/undefined)
 * @param {any}  liveQ                contracts quote-bag entry (may be null/undefined)
 * @returns {boolean}
 */
function _applyQuoteFields(row, snap, liveQ) {
  const snapLtp = snap?.ltp;
  if (snapLtp != null) {
    row.ltp        = snapLtp;
    row.bid        = snap.bid        ?? liveQ?.bid        ?? row.bid        ?? null;
    row.ask        = snap.ask        ?? liveQ?.ask        ?? row.ask        ?? null;
    row.open       = snap.open       ?? liveQ?.open       ?? row.open       ?? null;
    row.close      = snap.close      ?? liveQ?.close      ?? row.close      ?? null;
    row.change     = snap.day_change     ?? liveQ?.change     ?? row.change     ?? null;
    row.change_pct = snap.day_change_pct ?? liveQ?.change_pct ?? row.change_pct ?? null;
    row.volume     = snap.volume ?? liveQ?.volume ?? row.volume ?? null;
    row.oi         = snap.oi     ?? liveQ?.oi     ?? row.oi     ?? null;
    return true;
  }
  if (liveQ?.ltp) {
    row.ltp        = liveQ.ltp;
    row.bid        = liveQ.bid        ?? row.bid        ?? null;
    row.ask        = liveQ.ask        ?? row.ask        ?? null;
    row.open       = liveQ.open       ?? row.open       ?? null;
    row.close      = liveQ.close      ?? row.close      ?? null;
    row.change     = liveQ.change     ?? row.change     ?? null;
    row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
    row.volume     = liveQ.volume     ?? row.volume     ?? null;
    row.oi         = liveQ.oi         ?? row.oi         ?? null;
    return true;
  }
  return false; // caller handles its own no-data fallback
}

// ── Section helpers ───────────────────────────────────────────────────────────

/**
 * Apply quote fields from a symbolStore `snap` onto a watchlist row.
 * Always writes all nine fields (null when snap has no value) so grid
 * cells never see `undefined`. Watchlist rows have no liveQ bag.
 *
 * @param {Record<string, any>} row   target row (mutated)
 * @param {any}  snap                 symbolStore snapshot (may be null/undefined)
 */
function _applyWatchlistQuoteFields(row, snap) {
  row.ltp        = snap?.ltp            ?? row.ltp        ?? null;
  row.bid        = snap?.bid            ?? row.bid        ?? null;
  row.ask        = snap?.ask            ?? row.ask        ?? null;
  row.open       = snap?.open           ?? row.open       ?? null;
  row.close      = snap?.close          ?? row.close      ?? null;
  row.change     = snap?.day_change     ?? row.change     ?? null;
  row.change_pct = snap?.day_change_pct ?? row.change_pct ?? null;
  row.volume     = snap?.volume         ?? row.volume     ?? null;
  row.oi         = snap?.oi             ?? row.oi         ?? null;
}

/**
 * Apply quote fields from an underlying-quote bag `q` onto an anchor row.
 * Split write policy: ltp/bid/ask always overwrite; close/change/open/
 * high/low/volume/oi only-if-null. Includes high+low (not in _applyQuoteFields).
 * Field names: q.change_pct / q.change (not q.day_change_pct) — anchor shape.
 *
 * @param {Record<string, any>} row   target row (mutated)
 * @param {any}  q                    underlying quote bag entry
 */
function _applyUnderlyingQuoteFields(row, q) {
  row.ltp        = q.ltp        ?? row.ltp        ?? null;
  row.bid        = q.bid        ?? row.bid        ?? null;
  row.ask        = q.ask        ?? row.ask        ?? null;
  if (row.close      == null) row.close      = q.close      ?? null;
  if (row.change     == null) row.change     = q.change     ?? null;
  if (row.change_pct == null) row.change_pct = q.change_pct ?? null;
  if (row.open       == null) row.open       = q.open       ?? null;
  if (row.high       == null) row.high       = q.high       ?? null;
  if (row.low        == null) row.low        = q.low        ?? null;
  if (row.volume     == null) row.volume     = q.volume     ?? null;
  if (row.oi         == null) row.oi         = q.oi         ?? null;
}

/**
 * Build the set of tradingsymbols already present in visible majors.
 * Used by mergeMoverRows to decide whether a mover symbol already has
 * a row (badge-only) vs needs a brand-new Movers-major row.
 *
 * @param {Record<string, any>} byKey
 * @param {boolean} includePos
 * @param {boolean} includeHold
 * @param {boolean} includeWatch
 * @returns {Set<string>}
 */
function _buildVisibleSymbolSet(byKey, includePos, includeHold, includeWatch) {
  const out = new Set();
  for (const row of Object.values(byKey)) {
    if (!row.tradingsymbol) continue;
    const mg = row._majorGroup;
    if (mg === 'positions' && !includePos)   continue;
    if (mg === 'holdings'  && !includeHold)  continue;
    if (mg === 'watchlist' && !includeWatch) continue;
    out.add(row.tradingsymbol);
  }
  return out;
}

/**
 * Badge every existing row for `sym` with mover metadata.
 * Applies to positions/holdings/watchlist rows that share the same symbol
 * as an incoming mover entry.
 *
 * @param {Record<string, any>} byKey
 * @param {string}              sym            UPPER tradingsymbol
 * @param {any}                 m              raw mover row
 * @param {number|null}         liveChangePct  pre-computed change_pct for this mover
 */
function _badgeExistingRowsForMover(byKey, sym, m, liveChangePct) {
  for (const row of Object.values(byKey)) {
    if (row.tradingsymbol !== sym) continue;
    row.src.m = true;
    row._mover_sticky     = m.sticky ?? row._mover_sticky     ?? false;
    row._mover_change_pct = liveChangePct ?? row._mover_change_pct ?? null;
    _propagateStaleAndSource(row, m);
  }
}

/**
 * Populate a dedicated Movers-major row from the raw mover entry + snap.
 * Caller is responsible for creating the row via `get(sym, 'movers')`.
 *
 * @param {Record<string, any>} row           target movers-major row (mutated)
 * @param {any}                 m             raw mover row
 * @param {any}                 snap          symbolStore snapshot (may be null)
 * @param {number|null}         liveLtp       pre-computed ltp for this mover
 * @param {number|null}         liveChangePct pre-computed change_pct
 * @param {number|null}         liveClose     pre-computed close price
 */
function _populateMoverRow(row, m, snap, liveLtp, liveChangePct, liveClose) {
  row.src.m = true;
  row.exchange      = row.exchange || m.exchange || 'NSE';
  row.tradingsymbol = String(m.tradingsymbol || '').toUpperCase();
  if (m.quote_symbol) row.quote_symbol = m.quote_symbol;
  if (row.ltp        == null && liveLtp       != null) row.ltp        = liveLtp;
  if (row.change_pct == null && liveChangePct != null) row.change_pct = liveChangePct;
  if (row.close      == null && liveClose     != null) row.close      = liveClose;
  if (liveClose != null && row.change == null && row.ltp != null)
    row.change = row.ltp - liveClose;
  if (row.open   == null && snap?.open   != null) row.open   = snap.open;
  if (row.high   == null && snap?.high   != null) row.high   = snap.high;
  if (row.low    == null && snap?.low    != null) row.low    = snap.low;
  if (row.volume == null && snap?.volume != null) row.volume = snap.volume;
  if (row.oi     == null && snap?.oi     != null) row.oi     = snap.oi;
  row._mover_sticky     = m.sticky ?? false;
  row._mover_change_pct = liveChangePct ?? null;
  if (m._moverGroups)    row._moverGroups    = m._moverGroups;
  if (m._moverGroup)     row._moverGroup     = m._moverGroup;
  row._moverDirection = m._moverDirection || ((m.change_pct ?? m._mover_change_pct ?? 0) >= 0 ? 'winners' : 'losers');
  _propagateStaleAndSource(row, m);
}

/**
 * Remove all Movers-major rows from byKey (called when includeMovers=false).
 *
 * @param {Record<string, any>} byKey
 */
function _stripMoversMajor(byKey) {
  for (const [k, row] of Object.entries(byKey)) {
    if (row._majorGroup === 'movers') delete byKey[k];
  }
}

/**
 * Section 1 — merge watchlist rows.
 *
 * Each list carries `is_pinned`; pinned lists → 'pinned' major, others
 * → 'watchlist' major. A symbol in both a pinned list AND a user list
 * creates two rows (one per major) — by design.
 *
 * @param {Record<string, any>} byKey
 * @param {any[]} actLists
 * @param {{ snapOf: (sym: string) => any, getInst: ((s: string) => any) | null }} ctx
 */
export function mergeWatchlistRows(byKey, actLists, ctx) {
  const get = makeRowFactory(byKey);
  const { snapOf, getInst } = ctx;
  for (const list of (actLists || [])) {
    const major = list?.is_pinned ? 'pinned' : 'watchlist';
    for (const it of (list?.items || [])) {
      const sym = String(it.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(sym, major);
      row.exchange      = row.exchange || it.exchange;
      row.tradingsymbol = sym;
      if (it.alias) row.display_name = String(it.alias);
      if (row.watchlist_item_id == null) {
        row.watchlist_item_id = it.id;
        row.watchlist_list_id = list.id;
      }
      row.src.w = true;
      if (major === 'pinned') row._fromPinnedList = true;
      const snap = snapOf(sym);
      _applyWatchlistQuoteFields(row, snap);
      fillSymbolMeta(row, sym, getInst);
      // Propagate is_animating / price_source from the symbolStore
      // snapshot (populated by publishWatchQuotes) so watchlist rows
      // don't default to animating during closed hours.
      if (snap) _propagateStaleAndSource(row, snap);
    }
  }
}

/**
 * Section 2 — merge position rows (major: 'positions').
 *
 * Multi-account positions for the same symbol merge into one row.
 * Includes the Day P&L recompute with market-open gate, realised-today
 * carry, Contract A branch, and price_source / is_animating propagation.
 *
 * @param {Record<string, any>} byKey
 * @param {any[]} pos
 * @param {boolean} includePos
 * @param {Record<string, any>} cq  contracts quote map
 * @param {{
 *   snapOf: (sym: string) => any,
 *   getInst: ((s: string) => any) | null,
 *   isMarketOpen: () => boolean,
 *   baseDayPnlForPosition: (r: any) => number,
 * }} ctx
 */
export function mergePositionRows(byKey, pos, includePos, cq, ctx) {
  if (includePos === false) return;
  const get = makeRowFactory(byKey);
  const { snapOf, getInst, isMarketOpen, baseDayPnlForPosition } = ctx;
  for (const r of pos) {
    const exch = r.exchange || 'NFO';
    const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const row = get(sym, 'positions');
    row.exchange      = row.exchange || exch;
    row.tradingsymbol = sym;
    row.src.p = true;
    const q   = Number(r.quantity) || 0;
    const avg = Number(r.average_price) || 0;
    row.qty_pos  += q;
    row._avg_num += avg * q;
    if (r.account) row.accounts.add(String(r.account));
    // Market fields — symbolStore first, then contracts quote bag fallback.
    const snap  = snapOf(sym);
    const liveQ = cq?.[`${exch}:${sym}`];
    const _hadLtp = _applyQuoteFields(row, snap, liveQ);
    if (!_hadLtp && row.ltp == null) {
      row.ltp = r.last_price ?? null;
    }
    // Day P&L recompute — delegates to livePositionDayPnl (nav.js SSOT).
    // Normalise raw broker field names to the canonical param bag so the
    // helper works identically for both Pulse and Derivatives consumers.
    const _mktOpen  = isMarketOpen();
    const legLiveLtp = (_mktOpen && Number(liveQ?.ltp) > 0) ? Number(liveQ.ltp) : null;
    row.day_pnl = (row.day_pnl ?? 0) + livePositionDayPnl(
      {
        closePx: Number(r.close_price) || 0,
        pollLtp: Number(r.last_price)  || 0,
        qty:     q,
        avg,
        dcvRow:  r,
      },
      legLiveLtp,
      { marketOpen: _mktOpen },
    );
    // Total P&L live recompute.
    if (legLiveLtp != null && avg > 0 && q !== 0) {
      row.pnl = (row.pnl ?? 0) + (legLiveLtp - avg) * q + (Number(r.realised) || 0);
    } else {
      row.pnl = (row.pnl ?? 0) + (Number(r.pnl) || 0);
    }
    // Mirror broker raw values for TOTAL footer parity.
    row._broker_pnl     = (row._broker_pnl     ?? 0) + (Number(r.pnl)             || 0);
    row._broker_day_pnl = (row._broker_day_pnl ?? 0) + baseDayPnlForPosition(r);
    _propagateStaleAndSource(row, r);
    fillSymbolMeta(row, sym, getInst);
  }
}

/**
 * Section 3 — merge holdings rows (major: 'holdings').
 *
 * @param {Record<string, any>} byKey
 * @param {any[]} hold
 * @param {boolean} includeHold
 * @param {Record<string, any>} cq  contracts quote map
 * @param {{
 *   snapOf: (sym: string) => any,
 *   getInst: ((s: string) => any) | null,
 *   isMarketOpen: () => boolean,
 * }} ctx
 */
export function mergeHoldingRows(byKey, hold, includeHold, cq, ctx) {
  if (includeHold === false) return;
  const get = makeRowFactory(byKey);
  const { snapOf, getInst, isMarketOpen } = ctx;
  for (const r of hold) {
    const exch = r.exchange || 'NSE';
    const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const row = get(sym, 'holdings');
    row.exchange      = row.exchange || exch;
    row.tradingsymbol = sym;
    row.src.h = true;
    const heldQty = Number(r.opening_quantity) || Number(r.quantity) || 0;
    row.qty_hold += heldQty;
    row._avg_hold_num += (Number(r.average_price) || 0) * heldQty;
    if (r.account) row.accounts.add(String(r.account));
    // Market fields — symbolStore first, then contracts quote bag fallback.
    const snap  = snapOf(sym);
    const liveQ = cq?.[`${exch}:${sym}`];
    const _snapLtp = snap?.ltp;
    const _hadLtp  = _applyQuoteFields(row, snap, liveQ);
    // Holdings snap-branch: close falls back to broker r.close_price when
    // snap.close / liveQ.close / row.close are all null (position branch
    // does NOT have this extra tail — holdings-specific).
    if (_snapLtp != null && row.close == null) {
      row.close = Number(r.close_price) ?? null;
    }
    if (!_hadLtp) {
      if (row.ltp == null) row.ltp = r.last_price ?? null;
      if (r.day_change != null && row.change == null)
        row.change = Number(r.day_change);
      if (r.day_change_percentage != null && row.change_pct == null)
        row.change_pct = Number(r.day_change_percentage);
      if (row.close == null && r.close_price != null)
        row.close = Number(r.close_price);
    }
    if (liveQ?.volume != null) row.volume = liveQ.volume;
    if (liveQ?.oi     != null) row.oi     = liveQ.oi;
    // Day P&L and total P&L — use snapshot LTP regardless of market-open state.
    const liveHold = (_snapLtp != null && Number(_snapLtp) > 0) ? Number(_snapLtp)
                   : (Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null);
    const holdClose = Number(r.close_price) || 0;
    const holdAvg   = Number(r.average_price) || 0;
    if (liveHold != null && holdClose > 0 && heldQty !== 0) {
      row.day_pnl = (row.day_pnl ?? 0) + (liveHold - holdClose) * heldQty;
    } else {
      // Holdings: day_change_val is correct (no new-position overnight_qty=0 edge case — holdings don't have intraday P&L splits)
      row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
    }
    if (liveHold != null && holdAvg > 0 && heldQty !== 0) {
      row.pnl = (row.pnl ?? 0) + (liveHold - holdAvg) * heldQty;
    } else {
      row.pnl = (row.pnl ?? 0) + (Number(r.pnl) || 0);
    }
    row._broker_pnl     = (row._broker_pnl     ?? 0) + (Number(r.pnl)             || 0);
    row._broker_day_pnl = (row._broker_day_pnl ?? 0) + (Number(r.day_change_val) || 0);
    _propagateStaleAndSource(row, r);
    fillSymbolMeta(row, sym, getInst);
  }
}

/**
 * Section 4 — merge option-underlying anchor rows.
 *
 * Anchors group option positions/holdings under their underlying symbol.
 * Gate: only create an anchor in a major when that major's toggle is on
 * AND at least one CE/PE is present in the scoped input for that major.
 *
 * @param {Record<string, any>} byKey
 * @param {Record<string, any>} uq  underlyings quote map (from pulseQuotes)
 * @param {any[]} pos
 * @param {any[]} hold
 * @param {boolean} includePos
 * @param {boolean} includeHold
 * @param {{ getInst: ((s: string) => any) | null }} ctx
 */
export function mergeUnderlyingAnchors(byKey, uq, pos, hold, includePos, includeHold, ctx) {
  const get = makeRowFactory(byKey);
  const { getInst } = ctx;
  // Scoped option-underlying sets — anchors are gated on presence of at least
  // one CE/PE within the enabled major's scoped rows.
  const posOptUnderlyings  = _optUnderlyingSet(includePos  === false ? [] : pos,  getInst);
  const holdOptUnderlyings = _optUnderlyingSet(includeHold === false ? [] : hold, getInst);
  for (const [, q] of Object.entries(uq)) {
    const info = q._resolved;
    if (!info) continue;
    const anchorMajor = info._major || 'positions';
    if (anchorMajor === 'positions' && includePos  === false) continue;
    if (anchorMajor === 'holdings'  && includeHold === false) continue;
    const _uKey = String(info.displayUnderlying || info.underlying_group || '').toUpperCase();
    if (anchorMajor === 'positions' && _uKey && !posOptUnderlyings.has(_uKey)) continue;
    if (anchorMajor === 'holdings'  && _uKey && !holdOptUnderlyings.has(_uKey)) continue;
    const row = get(info.tradingsymbol, anchorMajor);
    row.exchange      = row.exchange || info.exchange;
    row.tradingsymbol = info.tradingsymbol;
    row.src.u = true;
    row.underlying = info.displayUnderlying || info.underlying_group;
    row.kind       = info.kind;
    _applyUnderlyingQuoteFields(row, q);
  }
}

/**
 * Section 5 — merge mover rows.
 *
 * Every mover symbol creates one Movers-major row (keyed via `__mov`
 * suffix) regardless of membership in other majors. Symbols already
 * in other visible majors are additionally badged `src.m = true`.
 *
 * @param {Record<string, any>} byKey
 * @param {any[]} moverRows
 * @param {boolean} includeMovers
 * @param {boolean} includePos
 * @param {boolean} includeHold
 * @param {boolean} includeWatch
 * @param {{ snapOf: (sym: string) => any }} ctx
 */
export function mergeMoverRows(byKey, moverRows, includeMovers, includePos, includeHold, includeWatch, ctx) {
  const get = makeRowFactory(byKey);
  const { snapOf } = ctx;
  const existingSymbols = _buildVisibleSymbolSet(byKey, includePos, includeHold, includeWatch);
  for (const m of (moverRows || [])) {
    const sym = String(m.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const snap = snapOf(sym);
    const liveLtp = snap?.ltp ?? m.last_price ?? null;
    // Prefer moversStore-owned change_pct over symbolStore during closed hours
    // to avoid poll-to-poll oscillation from multiple publishers.
    const liveChangePct = m.change_pct ?? snap?.day_change_pct ?? null;
    const liveClose     = snap?.close  ?? m.previous_close     ?? null;
    if (existingSymbols.has(sym)) {
      _badgeExistingRowsForMover(byKey, sym, m, liveChangePct);
    }
    // Create dedicated Movers-major row (both pure movers AND symbols
    // already in other majors get one via the __mov key).
    _populateMoverRow(get(sym, 'movers'), m, snap, liveLtp, liveChangePct, liveClose);
  }
  // Strip Movers-major rows when showMovers is off.
  if (!includeMovers) _stripMoversMajor(byKey);
}

/**
 * Section 6 — tag watched index symbols with their canonical underlying.
 *
 * Re-tags tradingsymbol → underlying so the sort groups indices with
 * their derivative chains.
 *
 * @param {Record<string, any>} byKey
 */
export function tagWatchedIndices(byKey) {
  for (const row of Object.values(byKey)) {
    const tag = INDEX_TO_UNDERLYING[String(row.tradingsymbol || '').toUpperCase()];
    if (tag) {
      row.underlying = tag;
      row.kind       = 'spot';
    }
  }
}

/**
 * Section 7 — finalize rows: weighted averages, combined avg, inv/cur
 * value, day_pct, _prev_market_value, _acctColor. Cleans up temp fields.
 *
 * @param {Record<string, any>} byKey
 * @param {{
 *   directional: (changePct: number|null, qty: number) => number|null,
 *   leadAccount: (row: any) => string|null,
 *   acctColor: (acct: string) => string|null,
 * }} ctx
 */
export function finalizeRows(byKey, ctx) {
  const { directional, leadAccount, acctColor } = ctx;
  for (const row of Object.values(byKey)) {
    if (row.qty_pos !== 0) {
      row.avg_pos = row._avg_num / row.qty_pos;
    }
    if (row.qty_hold !== 0) {
      row.avg_hold = row._avg_hold_num / row.qty_hold;
    }
    const posCost  = Math.abs(row._avg_num);
    const holdCost = Math.abs(row._avg_hold_num);
    const denom    = Math.abs(row.qty_pos) + Math.abs(row.qty_hold);
    row.avg_combined = denom > 0 ? (posCost + holdCost) / denom : null;
    row._cost_basis  = posCost + holdCost;
    const heldAbs = Math.abs(row.qty_hold);
    const ltpNum  = Number(row.ltp);
    row.inv_val = heldAbs > 0 ? holdCost : null;
    row.cur_val = (heldAbs > 0 && Number.isFinite(ltpNum) && ltpNum > 0)
      ? ltpNum * heldAbs
      : null;
    delete row._avg_num;
    delete row._avg_hold_num;
    const netQty = (Number(row.qty_pos) || 0) + (Number(row.qty_hold) || 0);
    row.day_pct = directional(row.change_pct, netQty);
    const closeVal = Number(row.close) || 0;
    row._prev_market_value = closeVal > 0
      ? closeVal * (Math.abs(row.qty_pos) + Math.abs(row.qty_hold))
      : 0;
    const _lead = leadAccount(row);
    row._acctColor = _lead ? acctColor(_lead) : null;
  }
}

/**
 * Section 8 — sort the unified row array.
 *
 * Sort key priority: groupBucket → manual groupOrder → localeCompare →
 * tier (spot/fut/opt) → strike ASC → CE before PE.
 *
 * @param {any[]} out  flat array from Object.values(byKey)
 * @param {Record<string, number>} groupOrder  operator manual overrides
 * @param {string[]} detachedSymbols
 * @returns {any[]}  mutates and returns out
 */
// Kind → tier rank for sortUnifiedRows. spot before fut before opt.
const _TIER_RANK = { spot: 0, fut: 1, opt: 2 };

// Opt-type rank within same-strike option pair. CE before PE, else last.
const _OPT_TYPE_RANK = { CE: 0, PE: 1 };

/**
 * Compare two rows within the SAME group (same underlying, same bucket).
 * Ordering: tier ASC → within opt: strike ASC → CE before PE → symbol alpha.
 *
 * @param {any} a
 * @param {any} b
 * @returns {number}
 */
function _compareSameGroup(a, b) {
  const ta = _TIER_RANK[a.kind] ?? 3;
  const tb = _TIER_RANK[b.kind] ?? 3;
  if (ta !== tb) return ta - tb;
  if (a.kind === 'opt' && b.kind === 'opt') {
    const sa = a.strike ?? 0, sb = b.strike ?? 0;
    if (sa !== sb) return sa - sb;
    const oa = _OPT_TYPE_RANK[a.opt_type] ?? 2;
    const ob = _OPT_TYPE_RANK[b.opt_type] ?? 2;
    return oa - ob;
  }
  return (a.tradingsymbol || '').localeCompare(b.tradingsymbol || '');
}

/**
 * Compare two DIFFERENT groups by operator manual `order` map.
 * Ordered groups win over unordered ones (which then fall back to alpha).
 *
 * @param {string} ga
 * @param {string} gb
 * @param {(g: string) => number|null} rankOf
 * @returns {number}
 */
function _compareGroups(ga, gb, rankOf) {
  const ra = rankOf(ga), rb = rankOf(gb);
  if (ra != null && rb != null && ra !== rb) return ra - rb;
  if (ra != null && rb == null) return -1;
  if (ra == null && rb != null) return  1;
  return ga.localeCompare(gb);
}

/**
 * Assign a src-based bucket rank to a row.
 *   watchlist:1, positions:2, holdings:3, other:4
 * Called per-row when building the group→minBucket map.
 *
 * @param {any} r
 * @returns {number}
 */
function _srcBucket(r) {
  if (r.src?.w) return 1;
  if (r.src?.p) return 2;
  if (r.src?.h) return 3;
  return 4;
}

export function sortUnifiedRows(out, groupOrder, detachedSymbols) {
  const detachedSet = new Set((detachedSymbols || []).map(s => s.toUpperCase()));
  const groupKey = (r) => {
    const sym = String(r.tradingsymbol || '').toUpperCase();
    if (detachedSet.has(sym)) return `__DETACHED__${sym}`;
    return r.underlying || `~~${r.tradingsymbol || ''}`;
  };
  // Group → minimum src-bucket rank. Every group inherits its most-privileged
  // member's bucket (watchlist beats positions beats holdings beats other).
  const groupBucket = {};
  for (const r of out) {
    const g = String(groupKey(r));
    const bucket = _srcBucket(r);
    if (groupBucket[g] == null || bucket < groupBucket[g]) {
      groupBucket[g] = bucket;
    }
  }
  const order = groupOrder || {};
  const rankOf = (g) => {
    const u = String(g || '').toUpperCase();
    return order[u] != null ? order[u] : null;
  };
  out.sort((a, b) => {
    const ga = String(groupKey(a)), gb = String(groupKey(b));
    const ba = groupBucket[ga] ?? 2, bb = groupBucket[gb] ?? 2;
    if (ba !== bb) return ba - bb;
    if (ga !== gb) return _compareGroups(ga, gb, rankOf);
    return _compareSameGroup(a, b);
  });
  return out;
}
