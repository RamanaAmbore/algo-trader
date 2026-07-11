/**
 * rootOf — virtual first-class symbol display helper.
 *
 * MCX/CDS futures never show their raw contract name (e.g. CRUDEOIL26JUNFUT)
 * in the UI. Instead we map them to virtual roots:
 *
 *   CRUDEOIL26JUNFUT  (front-month) → "CRUDEOIL"
 *   CRUDEOIL26JULFUT  (back-month)  → "CRUDEOIL_NEXT"
 *   CRUDEOIL26AUGFUT  (far-month)   → "CRUDEOIL26AUGFUT"  ← pass-through (raw)
 *
 * Two entry points:
 *
 *   rootOf(contract, exchange)        → virtual root string (pure, sync)
 *   rootOfLabel(contract, exchange)   → display label (e.g. "GOLDM.NEXT", "CRUDEOIL")
 *
 * The pure JS implementation mirrors backend/api/algo/symbol_resolver.py's
 * root_of() function and uses the same two-slot instrument store that
 * symbolStore.svelte.js maintains (front-month at slot 0, back-month at
 * slot 1). When the store hasn't loaded yet the function falls back to
 * pass-through (returns the raw contract), so no spinner is needed.
 *
 * Usage
 * -----
 *   import { rootOf, rootOfLabel } from '$lib/data/rootOf.js';
 *
 *   // In a cell renderer:
 *   const display = rootOf(row.tradingsymbol, row.exchange);
 *
 *   // In a label chip:
 *   const label = rootOfLabel(row.tradingsymbol, row.exchange);
 *
 * Seeding the front/back-month map
 * ----------------------------------
 * The module exports `seedRootMap(mcxMap, cdsMap)` where each argument is
 * a plain object keyed by root (e.g. { CRUDEOIL: ['CRUDEOIL26JUNFUT', 'CRUDEOIL26JULFUT'] }).
 * `frontend/src/lib/data/instruments.js` calls this once after the
 * instruments cache is warm (or after each refresh cycle).
 *
 * Alternatively, call `seedRootMapFromInstruments(items)` which iterates
 * the full instruments array and builds the map in one pass.
 *
 * Reactivity
 * ----------
 * The internal maps are plain JS objects (not Svelte stores). Components that
 * need reactivity should bind to the instruments-cache version stamp and
 * re-derive the label inside a `$derived()`.
 */

import { displaySymbol } from './displaySymbol.js';

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------

/**
 * @type {Object.<string, string[]>}  root → [front, back?]   (MCX)
 */
let _mcx = {};

/**
 * @type {Object.<string, string[]>}  root → [front, back?]   (CDS)
 */
let _cds = {};

// ---------------------------------------------------------------------------
// Seeding API
// ---------------------------------------------------------------------------

/**
 * Seed the front/back-month contract maps from pre-computed objects.
 *
 * @param {Object.<string, string[]>} mcxMap  { CRUDEOIL: ['CRUDEOIL26JUNFUT', 'CRUDEOIL26JULFUT'], ... }
 * @param {Object.<string, string[]>} cdsMap  { USDINR: ['USDINR26JUNFUT', ...], ... }
 */
export function seedRootMap(mcxMap, cdsMap) {
  _mcx = mcxMap || {};
  _cds = cdsMap || {};
}

/**
 * Build and seed the root maps from a flat instruments array in one pass.
 * Keeps only the two nearest active contracts per root (sorted ascending
 * by expiry field `x`).
 *
 * @param {Array<{s: string, e: string, t: string, u?: string, x?: string}>} items
 *   The `items` array from InstrumentsResponse (same shape as Instrument msgspec.Struct).
 */
export function seedRootMapFromInstruments(items) {
  if (!Array.isArray(items)) return;

  const todayIso = _todayIso();

  /** @type {Object.<string, Array<{s: string, x: string}>>} */
  const mcxBuild = {};
  /** @type {Object.<string, Array<{s: string, x: string}>>} */
  const cdsBuild = {};

  for (const inst of items) {
    if (inst.t !== 'FUT' || !inst.u || !inst.x) continue;
    // Only active contracts (not settling today)
    if (inst.x <= todayIso) continue;
    const root = inst.u.toUpperCase();
    if (inst.e === 'MCX') {
      if (!mcxBuild[root]) mcxBuild[root] = [];
      mcxBuild[root].push({ s: inst.s, x: inst.x });
    } else if (inst.e === 'CDS') {
      if (!cdsBuild[root]) cdsBuild[root] = [];
      cdsBuild[root].push({ s: inst.s, x: inst.x });
    }
  }

  // Sort ascending by expiry, keep first two
  /** @param {Object.<string, Array<{s: string, x: string}>>} build */
  function _compact(build) {
    /** @type {Object.<string, string[]>} */
    const out = {};
    for (const [root, contracts] of Object.entries(build)) {
      contracts.sort((a, b) => (a.x < b.x ? -1 : a.x > b.x ? 1 : 0));
      out[root] = contracts.slice(0, 2).map((c) => c.s);
    }
    return out;
  }

  _mcx = _compact(mcxBuild);
  _cds = _compact(cdsBuild);
}

// ---------------------------------------------------------------------------
// Core helpers
// ---------------------------------------------------------------------------

/** @returns {string}  Today's date as YYYY-MM-DD in IST (matches backend _ist_today_iso) */
function _todayIso() {
  return new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

// Kite FUT pattern: ROOT + YY + MON + FUT   e.g. CRUDEOIL26JUNFUT
const _FUT_RE = /^([A-Z]+)\d{2}[A-Z]{3}FUT$/i;

/**
 * Extract the raw root from a Kite futures tradingsymbol.
 * Returns null for non-futures symbols.
 *
 * @param {string} contract
 * @returns {string|null}
 */
function _futRoot(contract) {
  const m = _FUT_RE.exec((contract || '').toUpperCase());
  return m ? m[1] : null;
}

/**
 * Lookup the contract map for a given exchange.
 *
 * @param {string} exchange
 * @returns {Object.<string, string[]>}
 */
function _mapFor(exchange) {
  const e = (exchange || '').toUpperCase();
  if (e === 'MCX') return _mcx;
  if (e === 'CDS') return _cds;
  return {};
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Map a real MCX/CDS futures contract to its virtual display root.
 *
 * CRUDEOIL26JUNFUT (front-month) → "CRUDEOIL"
 * CRUDEOIL26JULFUT (back-month)  → "CRUDEOIL_NEXT"
 * CRUDEOIL26AUGFUT (far-month)   → "CRUDEOIL26AUGFUT"  (pass-through)
 * RELIANCE                       → "RELIANCE"           (pass-through)
 *
 * @param {string} contract  Kite tradingsymbol
 * @param {string} exchange  Exchange (MCX / CDS / …)
 * @returns {string}         Virtual root or the original contract
 */
export function rootOf(contract, exchange) {
  if (!contract) return contract || '';
  const exch = (exchange || '').toUpperCase();
  if (exch !== 'MCX' && exch !== 'CDS') return contract;

  const root = _futRoot(contract);
  if (!root) return contract;  // options / equities pass through

  const slots = _mapFor(exch)[root];
  if (!slots || slots.length === 0) return contract;  // map not seeded

  const cu = contract.toUpperCase();
  if (slots[0] && slots[0].toUpperCase() === cu) return root;
  if (slots[1] && slots[1].toUpperCase() === cu) return `${root}_NEXT`;
  return contract;  // far-month — pass through raw
}

/**
 * Human-readable label for the virtual root.
 *
 * "CRUDEOIL"         → "CRUDEOIL"
 * "CRUDEOIL_NEXT"    → "CRUDEOIL.NEXT"   (dot separator, operator spec 2026-07-03)
 * "CRUDEOIL26JUNFUT" (far-month) → "CRUDEOIL26JUNFUT"
 *
 * Internal machine key stays `_NEXT` (symbolStore keys, API bodies, rootOf()).
 * The dot form is render-only — applied here via displaySymbol().
 *
 * @param {string} contract
 * @param {string} exchange
 * @returns {string}
 */
export function rootOfLabel(contract, exchange) {
  const r = rootOf(contract, exchange);
  return displaySymbol(r);
}

/**
 * Resolve a virtual root to the real contract (forward direction).
 * Uses the seeded map — no async fetch needed.
 *
 * "CRUDEOIL"      → "CRUDEOIL26JUNFUT"  (front-month slot)
 * "CRUDEOIL_NEXT" → "CRUDEOIL26JULFUT"  (back-month slot)
 * Non-virtual symbols pass through unchanged.
 *
 * @param {string} virtual   Virtual root or any tradingsymbol
 * @param {string} exchange  MCX / CDS / …
 * @returns {string}         Real tradingsymbol, or virtual if not in map
 */
export function resolveVirtual(virtual, exchange) {
  if (!virtual) return virtual || '';
  const exch = (exchange || '').toUpperCase();
  if (exch !== 'MCX' && exch !== 'CDS') return virtual;

  const [base, suffix] = virtual.toUpperCase().endsWith('_NEXT')
    ? [virtual.toUpperCase().slice(0, -5), 'next']
    : [virtual.toUpperCase(), 'front'];

  // Only bare alpha strings are virtual (real contracts have digits)
  if (!/^[A-Z]+$/.test(base)) return virtual;

  const slots = _mapFor(exch)[base];
  if (!slots || slots.length === 0) return virtual;

  return suffix === 'next'
    ? (slots[1] ?? slots[0] ?? virtual)
    : (slots[0] ?? virtual);
}

// ---------------------------------------------------------------------------
// Virtual-root catalogue for search augmentation
// ---------------------------------------------------------------------------

/**
 * Return all virtual root entries for the given exchange, sorted by name.
 * Each entry is a synthetic instrument row suitable for injecting into
 * searchByPrefix results.
 *
 * Shape matches the compact Instrument format used by instruments.js:
 *   { s: 'GOLD', e: 'MCX', t: 'FUT', u: 'GOLD', virtual: true }
 *   { s: 'GOLD_NEXT', e: 'MCX', t: 'FUT', u: 'GOLD', virtual: true }
 *
 * Only roots that appear in the seeded front-month map are returned —
 * no synthetic entry for a root that has no active contracts.
 *
 * @param {string} exchange  'MCX' or 'CDS'
 * @returns {Array<{s: string, e: string, t: string, u: string, virtual: boolean}>}
 */
export function getVirtualRoots(exchange) {
  const exch = (exchange || '').toUpperCase();
  const map = _mapFor(exch);
  const out = [];
  for (const root of Object.keys(map).sort()) {
    const slots = map[root];
    if (!slots || slots.length === 0) continue;
    // Front-month virtual root
    out.push({ s: root, e: exch, t: 'FUT', u: root, virtual: true });
    // Back-month virtual root — only when a back-month slot exists
    if (slots[1]) {
      out.push({ s: `${root}_NEXT`, e: exch, t: 'FUT', u: root, virtual: true });
    }
  }
  return out;
}
