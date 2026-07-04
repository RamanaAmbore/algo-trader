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

// ── Section helpers ───────────────────────────────────────────────────────────

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
      row.ltp        = snap?.ltp            ?? row.ltp        ?? null;
      row.bid        = snap?.bid            ?? row.bid        ?? null;
      row.ask        = snap?.ask            ?? row.ask        ?? null;
      row.close      = snap?.close          ?? row.close      ?? null;
      row.open       = snap?.open           ?? row.open       ?? null;
      row.change     = snap?.day_change     ?? row.change     ?? null;
      row.change_pct = snap?.day_change_pct ?? row.change_pct ?? null;
      row.volume     = snap?.volume         ?? row.volume     ?? null;
      row.oi         = snap?.oi             ?? row.oi         ?? null;
      fillSymbolMeta(row, sym, getInst);
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
    const snapLtp = snap?.ltp;
    if (snapLtp != null) {
      row.ltp        = snapLtp;
      row.bid        = snap.bid    ?? liveQ?.bid    ?? row.bid    ?? null;
      row.ask        = snap.ask    ?? liveQ?.ask    ?? row.ask    ?? null;
      row.open       = snap.open   ?? liveQ?.open   ?? row.open   ?? null;
      row.close      = snap.close  ?? liveQ?.close  ?? row.close  ?? null;
      row.change     = snap.day_change     ?? liveQ?.change     ?? row.change     ?? null;
      row.change_pct = snap.day_change_pct ?? liveQ?.change_pct ?? row.change_pct ?? null;
      row.volume     = snap.volume ?? liveQ?.volume ?? row.volume ?? null;
      row.oi         = snap.oi     ?? liveQ?.oi     ?? row.oi     ?? null;
    } else if (liveQ?.ltp) {
      row.ltp        = liveQ.ltp;
      row.bid        = liveQ.bid ?? row.bid ?? null;
      row.ask        = liveQ.ask ?? row.ask ?? null;
      row.open       = liveQ.open  ?? row.open  ?? null;
      row.close      = liveQ.close ?? row.close ?? null;
      row.change     = liveQ.change     ?? row.change     ?? null;
      row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
      row.volume     = liveQ.volume ?? row.volume ?? null;
      row.oi         = liveQ.oi     ?? row.oi     ?? null;
    } else if (row.ltp == null) {
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
    // Account-level staleness propagation.
    if (r.account_stale === true) {
      row.account_stale = true;
      if (r.account_stale_since) row.account_stale_since = r.account_stale_since;
    }
    // price_source / is_animating propagation.
    const _rps = r.price_source ?? r.ltp_source;
    if (_rps && _rps !== 'live') {
      row.price_source = row.price_source && row.price_source !== 'live'
        ? row.price_source : _rps;
    }
    if (r.is_animating === false) row.is_animating = false;
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
    const snapLtp = snap?.ltp;
    if (snapLtp != null) {
      row.ltp        = snapLtp;
      row.bid        = snap.bid    ?? liveQ?.bid    ?? row.bid    ?? null;
      row.ask        = snap.ask    ?? liveQ?.ask    ?? row.ask    ?? null;
      row.open       = snap.open   ?? liveQ?.open   ?? row.open   ?? null;
      row.close      = snap.close  ?? liveQ?.close  ?? row.close  ?? Number(r.close_price) ?? null;
      row.change     = snap.day_change     ?? liveQ?.change     ?? row.change     ?? null;
      row.change_pct = snap.day_change_pct ?? liveQ?.change_pct ?? row.change_pct ?? null;
      if (snap.volume != null) row.volume = snap.volume;
      if (snap.oi     != null) row.oi     = snap.oi;
    } else if (liveQ?.ltp) {
      row.ltp        = liveQ.ltp;
      row.bid        = liveQ.bid ?? row.bid ?? null;
      row.ask        = liveQ.ask ?? row.ask ?? null;
      row.open       = liveQ.open  ?? row.open  ?? null;
      row.close      = liveQ.close ?? row.close ?? null;
      row.change     = liveQ.change     ?? row.change     ?? null;
      row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
    } else {
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
    // Day P&L and total P&L recompute with market-open gate.
    const _holdMktOpen = isMarketOpen();
    const liveHold = (_holdMktOpen && (snapLtp != null && Number(snapLtp) > 0)) ? Number(snapLtp)
                   : (_holdMktOpen && Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null);
    const holdClose = Number(r.close_price) || 0;
    const holdAvg   = Number(r.average_price) || 0;
    if (liveHold != null && holdClose > 0 && heldQty !== 0) {
      row.day_pnl = (row.day_pnl ?? 0) + (liveHold - holdClose) * heldQty;
    } else {
      row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
    }
    if (liveHold != null && holdAvg > 0 && heldQty !== 0) {
      row.pnl = (row.pnl ?? 0) + (liveHold - holdAvg) * heldQty;
    } else {
      row.pnl = (row.pnl ?? 0) + (Number(r.pnl) || 0);
    }
    row._broker_pnl     = (row._broker_pnl     ?? 0) + (Number(r.pnl)             || 0);
    row._broker_day_pnl = (row._broker_day_pnl ?? 0) + (Number(r.day_change_val) || 0);
    // Account-level staleness propagation.
    if (r.account_stale === true) {
      row.account_stale = true;
      if (r.account_stale_since) row.account_stale_since = r.account_stale_since;
    }
    // price_source / is_animating propagation.
    const _rps = r.price_source ?? r.ltp_source;
    if (_rps && _rps !== 'live') {
      row.price_source = row.price_source && row.price_source !== 'live'
        ? row.price_source : _rps;
    }
    if (r.is_animating === false) row.is_animating = false;
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
  // Build scoped option-underlying sets.
  const posOptUnderlyings = new Set();
  for (const r of (includePos === false ? [] : pos)) {
    const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
    if (!/(CE|PE)$/i.test(sym)) continue;
    const p = parseSymbol(sym, getInst);
    if (p?.underlying) posOptUnderlyings.add(String(p.underlying).toUpperCase());
  }
  const holdOptUnderlyings = new Set();
  for (const r of (includeHold === false ? [] : hold)) {
    const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
    if (!/(CE|PE)$/i.test(sym)) continue;
    const p = parseSymbol(sym, getInst);
    if (p?.underlying) holdOptUnderlyings.add(String(p.underlying).toUpperCase());
  }
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
    row.underlying    = info.displayUnderlying || info.underlying_group;
    row.kind          = info.kind;
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
  // Build existing-symbols set scoped to currently-visible majors.
  const existingSymbols = new Set();
  for (const row of Object.values(byKey)) {
    if (!row.tradingsymbol) continue;
    const mg = row._majorGroup;
    if (mg === 'positions' && !includePos)  continue;
    if (mg === 'holdings'  && !includeHold) continue;
    if (mg === 'watchlist' && !includeWatch) continue;
    existingSymbols.add(row.tradingsymbol);
  }
  for (const m of (moverRows || [])) {
    const sym = String(m.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const snap = snapOf(sym);
    const liveLtp       = snap?.ltp            ?? m.last_price ?? null;
    // Prefer moversStore-owned change_pct over symbolStore during closed hours
    // to avoid poll-to-poll oscillation from multiple publishers.
    const liveChangePct = m.change_pct ?? snap?.day_change_pct ?? null;
    const liveClose     = snap?.close          ?? m.previous_close ?? null;
    if (existingSymbols.has(sym)) {
      for (const row of Object.values(byKey)) {
        if (row.tradingsymbol === sym) {
          row.src.m = true;
          row._mover_sticky     = m.sticky ?? row._mover_sticky     ?? false;
          row._mover_change_pct = liveChangePct ?? row._mover_change_pct ?? null;
        }
      }
    }
    // Create dedicated Movers-major row (both pure movers AND symbols
    // already in other majors get one via the __mov key).
    const row = get(sym, 'movers');
    row.src.m = true;
    row.exchange      = row.exchange || m.exchange || 'NSE';
    row.tradingsymbol = sym;
    if (m.quote_symbol) row.quote_symbol = m.quote_symbol;
    if (row.ltp == null && liveLtp != null)              row.ltp        = liveLtp;
    if (row.change_pct == null && liveChangePct != null) row.change_pct = liveChangePct;
    if (row.close == null && liveClose != null)          row.close      = liveClose;
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
    if (m._moverDirection) row._moverDirection = m._moverDirection;
  }
  // Strip Movers-major rows when showMovers is off.
  if (!includeMovers) {
    for (const [k, row] of Object.entries(byKey)) {
      if (row._majorGroup === 'movers') delete byKey[k];
    }
  }
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
export function sortUnifiedRows(out, groupOrder, detachedSymbols) {
  const detachedSet = new Set((detachedSymbols || []).map(s => s.toUpperCase()));
  const groupKey = (r) => {
    const sym = String(r.tradingsymbol || '').toUpperCase();
    if (detachedSet.has(sym)) return `__DETACHED__${sym}`;
    return r.underlying || `~~${r.tradingsymbol || ''}`;
  };
  const tierRank = (r) => {
    if (r.kind === 'spot') return 0;
    if (r.kind === 'fut')  return 1;
    if (r.kind === 'opt')  return 2;
    return 3;
  };
  const optTypeRank = (r) => (r.opt_type === 'CE' ? 0 : r.opt_type === 'PE' ? 1 : 2);
  const groupBucket = {};
  for (const r of out) {
    const g = String(groupKey(r));
    const bucket = r.src?.w ? 1
                 : r.src?.p ? 2
                 : r.src?.h ? 3
                 : 4;
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
    if (ga !== gb) {
      const ra = rankOf(ga), rb = rankOf(gb);
      if (ra != null && rb != null && ra !== rb) return ra - rb;
      if (ra != null && rb == null) return -1;
      if (ra == null && rb != null) return  1;
      return ga.localeCompare(gb);
    }
    const ta = tierRank(a), tb = tierRank(b);
    if (ta !== tb) return ta - tb;
    if (a.kind === 'opt' && b.kind === 'opt') {
      const sa = a.strike ?? 0, sb = b.strike ?? 0;
      if (sa !== sb) return sa - sb;
      return optTypeRank(a) - optTypeRank(b);
    }
    return (a.tradingsymbol || '').localeCompare(b.tradingsymbol || '');
  });
  return out;
}
