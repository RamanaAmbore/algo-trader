import { writable } from 'svelte/store';
import { _setExpiryLookup } from './decomposeSymbol.js';
import { seedRootMapFromInstruments } from './rootOf.js';

/**
 * Reactive signal bumped once each time `_buildIndexes` finishes — i.e.
 * the instruments cache has just been (re)populated and `getInstrument`
 * is now ready to serve expiry / lot-size / exchange lookups.
 *
 * Why: Svelte 5 `$derived` only re-fires when its reactive dependencies
 * change. The legacy plain-let `_byTradingsymbol` is invisible to Svelte,
 * so `<LegLabel>` rendering before the cache loaded would compute
 * `getInstrument(sym) → null → bare "26JUN"` and NEVER re-render once
 * the cache populated (the operator saw CRUDEOIL options stuck at
 * "26JUN" with no day after my expiry-day fix landed). Components that
 * read instrument metadata inside a derivation now subscribe to this
 * store; the bump triggers their re-evaluation.
 */
export const instrumentsCacheVersion = writable(0);

// Instrument universe — loaded once per trading day, cached in IndexedDB.
// Exposes prefix search, symbol lookup, and option-chain helpers for the
// command-line autocomplete.
//
// Data source: GET /api/instruments (Kite master dump, ~90k rows).
// Field abbreviations match the API payload:
//   s  tradingsymbol
//   e  exchange
//   t  instrument_type (EQ / FUT / CE / PE)
//   u  underlying name
//   x  expiry (YYYY-MM-DD)
//   k  strike
//   ls lot_size
//   ts tick_size

const DB_NAME  = 'ramboq';
const STORE    = 'instruments';
const META_KEY = 'meta';
const ITEMS_KEY = 'items';
// Bump this when the index-building logic changes (e.g. _derivedUnderlying)
// OR when the backend instruments payload semantics change (e.g. MCX lot
// size overrides at v4 — Kite's instruments dump reports lot_size=1 for
// every MCX commodity, so the backend now applies hardcoded multipliers).
// v5: added _exchangesBySymbol multi-listing index — force a clean refetch
// so every browser rebuilds the index from a known-good items list rather
// than trusting partial state from a prior session's cache.
// v6: MCX lot-size override correctness fix (audit 2026-07-01) — browsers
// that haven't cleared IndexedDB since before v5 may hold stale lot_size=1
// rows for MCX options (CRUDEOIL CE/PE). Bumping forces a fresh fetch from
// /api/instruments so every browser gets the corrected lot_size values.
const INDEX_SCHEMA_VERSION = 6;

// Module-level runtime caches (rebuilt on each page load)
let _items            = null;  // full list
let _byTradingsymbol  = null;  // Map<string, Instrument>
let _exchangesBySymbol = null; // Map<string, string[]> — multi-listing index for dual-listed equities (RELIANCE on NSE+BSE, etc.)
let _underlyings      = null;  // Set<string>
let _underlyingsSorted = null; // sorted array for prefix scan
let _byUnderlyingType = null;  // Map<`${u}|${t}`, Instrument[]>
let _loadPromise      = null;

function _openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE);
      }
    };
    req.onerror = () => reject(req.error);
    req.onsuccess = () => resolve(req.result);
  });
}

async function _idbGet(key) {
  const db = await _openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly');
    const req = tx.objectStore(STORE).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function _idbPut(key, value) {
  const db = await _openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

function _derivedUnderlying(it) {
  // For options/futures, the Kite `name` field carries the full company name
  // ("INTERGLOBE AVIATION"), NOT the ticker. The ticker prefix is embedded in
  // the tradingsymbol. The underlying is the longest pure-letter prefix before
  // any digit. Works for all Kite tradingsymbol formats:
  //   RELIANCE26APR1360CE       → RELIANCE   (monthly option)
  //   NIFTY2640722700CE         → NIFTY      (weekly option — no month letters)
  //   NIFTY25APRFUT             → NIFTY      (monthly future)
  //   CRUDEOIL25APRFUT          → CRUDEOIL   (commodity future)
  //   BANKNIFTY26APR51500CE     → BANKNIFTY
  //   RELIANCE                   → RELIANCE   (equity)
  const m = it.s.match(/^([A-Z]+)/);
  return m ? m[1] : it.s;
}

function _buildIndexes(items) {
  _items = items;
  _byTradingsymbol = new Map();
  _exchangesBySymbol = new Map();
  _underlyings = new Set();
  _byUnderlyingType = new Map();

  for (const it of items) {
    _byTradingsymbol.set(it.s, it);
    // Multi-listing index — Kite's instruments dump has one row per
    // (tradingsymbol, exchange) pair, so dual-listed equities (IFCI,
    // RELIANCE, etc.) appear twice. The primary `_byTradingsymbol`
    // map is last-write-wins for backward compatibility; this side-
    // index preserves every exchange a symbol trades on so the order
    // ticket can let the operator pick NSE vs BSE for dual-listed
    // equities while locking it for single-exchange instruments.
    if (it.e) {
      const list = _exchangesBySymbol.get(it.s);
      if (list) {
        if (!list.includes(it.e)) list.push(it.e);
      } else {
        _exchangesBySymbol.set(it.s, [it.e]);
      }
    }
    const underlying = _derivedUnderlying(it);
    if (underlying) {
      _underlyings.add(underlying);
      const key = `${underlying}|${it.t}`;
      if (!_byUnderlyingType.has(key)) _byUnderlyingType.set(key, []);
      _byUnderlyingType.get(key).push(it);
    }
  }
  _underlyingsSorted = Array.from(_underlyings).sort();
  // Register the expiry lookup with decomposeSymbol so formatSymbol()
  // can append the DD to monthly contracts (e.g. NIFTY-26JUN26-22000-CE
  // instead of NIFTY-26JUN-22000-CE). Reads `inst.x` (YYYY-MM-DD).
  // Module-bound by reference — every formatSymbol call after this point
  // sees the expiry day, regardless of whether the caller is on Pulse /
  // Legs / Performance / etc.
  _setExpiryLookup((sym) => {
    const inst = _byTradingsymbol?.get(String(sym || '').toUpperCase());
    return inst?.x || null;
  });
  // Seed the virtual-root resolution map so rootOf() / rootOfLabel()
  // can map MCX/CDS front/back-month contracts to their virtual roots.
  seedRootMapFromInstruments(items);
  // Bump the cache-ready signal so subscribed `$derived` blocks re-fire
  // and pick up the now-available expiry / lot-size / exchange data.
  instrumentsCacheVersion.update((n) => n + 1);
}

function _todayIST() {
  // Match the API's cycle_date (date in Asia/Kolkata).
  const s = new Date().toLocaleString('en-CA', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit',
  });
  return s.replaceAll('/', '-'); // "2026-04-05"
}

async function _fetchAndCache() {
  const { fetchInstruments } = await import('$lib/api');
  const data = await fetchInstruments();
  // Store compact form + schema version so stale caches are invalidated
  await _idbPut(META_KEY, {
    cycle_date: data.cycle_date,
    count: data.count,
    cached_at: Date.now(),
    schema_version: INDEX_SCHEMA_VERSION,
  });
  await _idbPut(ITEMS_KEY, data.items);
  return data.items;
}

export async function loadInstruments({ forceRefresh = false } = {}) {
  if (_items && !forceRefresh) return _items;
  if (_loadPromise && !forceRefresh) return _loadPromise;

  _loadPromise = (async () => {
    let items = null;
    if (!forceRefresh) {
      try {
        const meta = await _idbGet(META_KEY);
        const today = _todayIST();
        if (meta && meta.cycle_date === today
            && meta.schema_version === INDEX_SCHEMA_VERSION) {
          items = await _idbGet(ITEMS_KEY);
        }
      } catch (e) { /* ignore — fall through to fetch */ }
    }
    if (!items) items = await _fetchAndCache();
    _buildIndexes(items);
    return items;
  })();

  try { return await _loadPromise; }
  finally { _loadPromise = null; }
}

// ---------------------------------------------------------------------------
// Search helpers
// ---------------------------------------------------------------------------

function _prefixMatch(sortedArr, prefix, limit = 20) {
  if (!prefix) return sortedArr.slice(0, limit);
  const p = prefix.toUpperCase();
  const out = [];
  // Binary search for the first element ≥ prefix
  let lo = 0, hi = sortedArr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sortedArr[mid] < p) lo = mid + 1; else hi = mid;
  }
  while (lo < sortedArr.length && sortedArr[lo].startsWith(p) && out.length < limit) {
    out.push(sortedArr[lo]);
    lo++;
  }
  return out;
}

/** Suggest underlying names matching the given prefix (case-insensitive). */
export function suggestUnderlyings(prefix, limit = 20) {
  if (!_underlyingsSorted) return [];
  return _prefixMatch(_underlyingsSorted, prefix, limit);
}

/**
 * List underlyings that have contracts of the given type (CE/PE/FUT/EQ),
 * filtered by prefix. Returns up to `limit` sorted matches.
 */
export function listUnderlyingsByType(type, prefix = '', limit = 20) {
  if (!_byUnderlyingType || !_underlyingsSorted) return [];
  const t = String(type).toUpperCase();
  const p = String(prefix || '').toUpperCase();
  const out = [];
  if (t === 'EQ') {
    // Equity: underlyings where the EQ instrument itself exists
    for (const u of _underlyingsSorted) {
      if (p && !u.startsWith(p)) continue;
      const eq = _byTradingsymbol && _byTradingsymbol.get(u);
      if (eq && eq.t === 'EQ') out.push(u);
      if (out.length >= limit) break;
    }
    return out;
  }
  // CE / PE / FUT: underlyings that have at least one contract of this type
  for (const u of _underlyingsSorted) {
    if (p && !u.startsWith(p)) continue;
    if (_byUnderlyingType.has(`${u}|${t}`)) out.push(u);
    if (out.length >= limit) break;
  }
  return out;
}

/** Look up a single tradingsymbol (exact match, case-insensitive). */
export function getInstrument(tradingsymbol) {
  if (!_byTradingsymbol) return null;
  return _byTradingsymbol.get(tradingsymbol.toUpperCase()) || null;
}

/** Every exchange a tradingsymbol trades on. Most symbols return
 *  one entry; dual-listed equities (RELIANCE / IFCI / etc.) return
 *  ['NSE', 'BSE']. Empty array on cache miss (instruments cache
 *  not loaded yet) so the OrderTicket can fall back to the static
 *  kind-appropriate list. */
export function listExchangesForSymbol(tradingsymbol) {
  if (!_exchangesBySymbol) return [];
  return _exchangesBySymbol.get(String(tradingsymbol || '').toUpperCase()) || [];
}

/**
 * Search instruments by tradingsymbol prefix (case-insensitive).
 * Equity / index symbols are surfaced first (shorter); contracts
 * (CE/PE/FUT) come second. Returns up to `limit` matches.
 * Used by the watchlist add-symbol typeahead.
 */
export async function searchByPrefix(prefix, limit = 12) {
  await loadInstruments();
  if (!_byTradingsymbol) return [];
  const p = String(prefix || '').toUpperCase();
  if (!p) return [];
  const eq = [];   // equities / indices first
  const ct = [];   // contracts after
  for (const [sym, inst] of _byTradingsymbol) {
    if (!sym.startsWith(p)) continue;
    if (inst.t === 'EQ' || inst.t === '') {
      eq.push(inst);
    } else {
      ct.push(inst);
    }
    if (eq.length + ct.length >= limit * 3) break;  // bound the walk
  }
  return [...eq, ...ct].slice(0, limit);
}

/** List option contracts for an underlying + type (CE/PE). Returns sorted by expiry then strike. */
export function listOptions(underlying, type) {
  if (!_byUnderlyingType) return [];
  const rows = _byUnderlyingType.get(`${underlying.toUpperCase()}|${type}`) || [];
  return rows.slice().sort((a, b) => {
    if (a.x !== b.x) return (a.x || '').localeCompare(b.x || '');
    return (a.k || 0) - (b.k || 0);
  });
}

/** List futures contracts for an underlying. */
export function listFutures(underlying) {
  if (!_byUnderlyingType) return [];
  return (_byUnderlyingType.get(`${underlying.toUpperCase()}|FUT`) || [])
    .slice().sort((a, b) => (a.x || '').localeCompare(b.x || ''));
}

/**
 * Returns the F&O lot size for a stock/index that has option contracts
 * listed. Reads from the nearest-expiry CE contract on the underlying.
 * Falls back to the FUT contract when no CE is available. Returns 0
 * when the symbol is NOT an option underlying — caller treats that as
 * "this holding is not F&O-eligible".
 *
 * Use case: the Holdings grid colour-codes rows where the stock is an
 * F&O underlying so the operator can spot opportunities to write
 * covered calls / cash-secured puts. A second colour highlights rows
 * where the held qty is ≥ one lot, meaning the operator could write a
 * covered call against the position right now.
 */
export function getOptionUnderlyingLot(tradingsymbol) {
  if (!_byUnderlyingType) return 0;
  const sym = String(tradingsymbol || '').toUpperCase();
  if (!sym) return 0;
  // Try CE first — most underlyings have both CE + PE, picking CE keeps
  // the lookup deterministic.
  const ceRows = _byUnderlyingType.get(`${sym}|CE`);
  if (ceRows && ceRows.length) {
    return Number(ceRows[0].ls || 0);
  }
  // Index futures (NIFTY, BANKNIFTY) — no spot equity, just FUT contract.
  // Used when the holding symbol IS an index like NIFTY 50.
  const futRows = _byUnderlyingType.get(`${sym}|FUT`);
  if (futRows && futRows.length) {
    return Number(futRows[0].ls || 0);
  }
  return 0;
}

/** Nearest upcoming expiry for an underlying+type. Returns YYYY-MM-DD or null. */
export function nearestExpiry(underlying, type) {
  const rows = listOptions(underlying, type);
  if (rows.length === 0) return null;
  const today = _todayIST();
  for (const r of rows) {
    if (r.x >= today) return r.x;
  }
  return rows[rows.length - 1].x;
}

/** List distinct expiries available for an underlying+type (sorted, today-onward in IST). */
export function listExpiries(underlying, type) {
  const rows = listOptions(underlying, type);
  const today = _todayIST();
  const set = new Set();
  for (const r of rows) if (r.x && r.x >= today) set.add(r.x);
  return Array.from(set).sort();
}

/** List strikes for an underlying+type+expiry (sorted). */
export function listStrikes(underlying, type, expiry) {
  if (!_byUnderlyingType) return [];
  const rows = _byUnderlyingType.get(`${underlying.toUpperCase()}|${type}`) || [];
  const strikes = rows.filter(r => r.x === expiry).map(r => r.k).filter(k => k != null);
  return Array.from(new Set(strikes)).sort((a, b) => a - b);
}

/** Find the option contract matching underlying+type+strike+expiry. */
export function findOption(underlying, type, strike, expiry) {
  if (!_byUnderlyingType) return null;
  const rows = _byUnderlyingType.get(`${underlying.toUpperCase()}|${type}`) || [];
  return rows.find(r => r.k === strike && r.x === expiry) || null;
}

/** Find the future contract for an underlying (nearest expiry).
 *  Uses `r.x > today` so the front-month is considered expired on its
 *  actual expiry date — the page rolls to the next contract immediately
 *  rather than quoting the expiring contract's last-traded price, which
 *  diverges from the new front-month (CRUDEOIL: old contract at ₹7000
 *  vs live front at ₹5800). Falls back to the last row only when all
 *  rows are expired — instrument-cache lag; caller should show a stale
 *  warning in that case.
 */
export function findNearestFuture(underlying) {
  const rows = listFutures(underlying);
  if (rows.length === 0) return null;
  const today = _todayIST();
  for (const r of rows) if (r.x > today) return r;
  return rows[rows.length - 1];
}

/** Find the equity instrument for a symbol. */
export function findEquity(symbol) {
  const inst = getInstrument(symbol);
  if (inst && inst.t === 'EQ') return inst;
  return null;
}

/** Returns true if the given underlying has option contracts. */
export function hasOptions(underlying) {
  if (!_byUnderlyingType) return false;
  return _byUnderlyingType.has(`${underlying.toUpperCase()}|CE`)
      || _byUnderlyingType.has(`${underlying.toUpperCase()}|PE`);
}

/** Returns true if the given underlying has futures. */
export function hasFutures(underlying) {
  if (!_byUnderlyingType) return false;
  return _byUnderlyingType.has(`${underlying.toUpperCase()}|FUT`);
}

/** Returns true if the given underlying has options OR futures (any F&O coverage). */
export function hasFNO(underlying) {
  return hasOptions(underlying) || hasFutures(underlying);
}
