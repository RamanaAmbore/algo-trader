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
const INDEX_SCHEMA_VERSION = 4;

// Module-level runtime caches (rebuilt on each page load)
let _items            = null;  // full list
let _byTradingsymbol  = null;  // Map<string, Instrument>
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
  _underlyings = new Set();
  _byUnderlyingType = new Map();

  for (const it of items) {
    _byTradingsymbol.set(it.s, it);
    const underlying = _derivedUnderlying(it);
    if (underlying) {
      _underlyings.add(underlying);
      const key = `${underlying}|${it.t}`;
      if (!_byUnderlyingType.has(key)) _byUnderlyingType.set(key, []);
      _byUnderlyingType.get(key).push(it);
    }
  }
  _underlyingsSorted = Array.from(_underlyings).sort();
}

function _todayIST() {
  // Match the API's cycle_date (date in Asia/Kolkata).
  const s = new Date().toLocaleString('en-CA', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit',
  });
  return s.replaceAll('/', '-'); // "2026-04-05"
}

async function _fetchAndCache() {
  // TODO(FIX-25): migrate to api.js _get once a fetchInstruments helper is
  // added. Dynamic import is required here because this file is used outside
  // the Svelte component lifecycle; a static api.js import would pull in
  // authStore at module level and risk SSR issues.
  const { authStore } = await import('$lib/stores');
  const token = authStore.getToken();
  const res = await fetch('/api/instruments/', {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`instruments fetch ${res.status}`);
  const data = await res.json();
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

/** Nearest upcoming expiry for an underlying+type. Returns YYYY-MM-DD or null. */
export function nearestExpiry(underlying, type) {
  const rows = listOptions(underlying, type);
  if (rows.length === 0) return null;
  const today = new Date().toISOString().slice(0, 10);
  for (const r of rows) {
    if (r.x >= today) return r.x;
  }
  return rows[rows.length - 1].x;
}

/** List distinct expiries available for an underlying+type (sorted). */
export function listExpiries(underlying, type) {
  const rows = listOptions(underlying, type);
  const set = new Set();
  for (const r of rows) if (r.x) set.add(r.x);
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

/** Find the future contract for an underlying (nearest expiry). */
export function findNearestFuture(underlying) {
  const rows = listFutures(underlying);
  if (rows.length === 0) return null;
  const today = new Date().toISOString().slice(0, 10);
  for (const r of rows) if (r.x >= today) return r;
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
