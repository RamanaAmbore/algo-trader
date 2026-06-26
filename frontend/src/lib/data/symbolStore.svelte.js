/**
 * symbolStore — symbol-centric reactive store (Slice BH1, dual-write phase).
 *
 * Goal: collapse the per-endpoint fan-out (positionsStore.last_price +
 * watchQuotesStore[item_id].ltp + liveLtp[sym] + moversStore[].last_price)
 * into a single Map<tradingsymbol, MarketSnapshot> so every grid + card
 * reads the same LTP / day_change / volume / OI for a given symbol.
 *
 * BH1 status — shadow-population only. Existing stores still serve UI;
 * symbolStore is dual-written by fetchers + SSE so its content can be
 * verified before any consumer is migrated to read from it (BH2) and
 * before the old stores' market fields are deleted (BH3).
 *
 * Per-field timestamp arbitration
 * ────────────────────────────────
 * SSE ticks (real-time) carry `ltp` only and ALWAYS win for the ltp field
 * regardless of poll lateness; polls carry the whole snapshot but the
 * broker's quote-at-fetch-start is older than an SSE tick that landed
 * 500ms ago. We track two stamps per symbol: `ltp_ts` and `snapshot_ts`.
 * Merges accept new `ltp` only if incoming `ltp_ts > stored.ltp_ts`;
 * snapshot fields only if `snapshot_ts > stored.snapshot_ts`. This kills
 * the SSE-tick-then-late-poll-clobber race that exists today across
 * liveLtp + watchQuotes + positionsStore.
 *
 * Persistence
 * ───────────
 * SvelteMap is in-memory. We mirror to localStorage as `{sym: snapshot}`
 * under `rbq.cache.md.symbolStore` so cold mounts paint from cache like
 * positions/holdings already do. Writes are debounced (500ms) to coalesce
 * SSE tick bursts. Prune-on-load drops entries with `touched_at` older
 * than 24h so the persisted blob stays bounded — a delisted symbol that
 * never re-appears in any source falls off after a day.
 */

import { browser } from '$app/environment';
import { SvelteMap } from 'svelte/reactivity';
import { cachedRead, cachedWrite, TTL } from './persistentCache.js';

const _STORE_KEY = 'md.symbolStore';
const _PRUNE_AGE_MS = 24 * 60 * 60 * 1000; // 24h since last touch → drop on load

/**
 * @typedef {object} MarketSnapshot
 * @property {number=}  ltp
 * @property {number=}  close
 * @property {number=}  open
 * @property {number=}  high
 * @property {number=}  low
 * @property {number=}  day_change
 * @property {number=}  day_change_pct
 * @property {number=}  volume
 * @property {number=}  oi
 * @property {number=}  bid
 * @property {number=}  ask
 * @property {string=}  exchange
 * @property {number}   ltp_ts        // epoch-ms of last LTP write
 * @property {number}   snapshot_ts   // epoch-ms of last snapshot-field write
 * @property {number}   touched_at    // max(ltp_ts, snapshot_ts) — prune key
 */

/**
 * The shared map. SvelteMap gives fine-grained reactivity: a
 * `$derived(symbolStore.get('TCS'))` only re-runs when TCS specifically
 * changes, not when any symbol changes. Cheap for the 200-500 symbol
 * working set we see in practice.
 *
 * @type {SvelteMap<string, MarketSnapshot>}
 */
export const symbolStore = new SvelteMap();

// ── Hydrate from localStorage ────────────────────────────────────────────
//
// Done at module-evaluation time so any consumer reading symbolStore.get(sym)
// before its onMount fires sees cached data immediately. Mirrors the
// positionsStore / holdingsStore pattern in createDataStore.
(function _hydrate() {
  if (!browser) return;
  try {
    const cached = cachedRead(_STORE_KEY);
    if (!cached?.value || typeof cached.value !== 'object') return;
    const now = Date.now();
    for (const [sym, snap] of Object.entries(cached.value)) {
      if (!snap || typeof snap !== 'object') continue;
      const touched = Number(/** @type {any} */ (snap).touched_at) || 0;
      if (touched && now - touched > _PRUNE_AGE_MS) continue;
      symbolStore.set(String(sym).toUpperCase(), /** @type {any} */ (snap));
    }
  } catch { /* localStorage unavailable (SSR / private mode) */ }
})();

// ── Debounced persistence ────────────────────────────────────────────────
//
// SSE tick bursts (10-30/sec under load) would write to localStorage every
// tick without throttling, which hits a known ~50ms write penalty on some
// browsers. Coalesce to one write per 500ms.

/** @type {ReturnType<typeof setTimeout> | null} */
let _persistTimer = null;
let _dirty = false;

function _schedulePersist() {
  if (!browser) return;
  _dirty = true;
  if (_persistTimer != null) return;
  _persistTimer = setTimeout(() => {
    _persistTimer = null;
    if (!_dirty) return;
    _dirty = false;
    try {
      /** @type {Record<string, MarketSnapshot>} */
      const out = {};
      for (const [sym, snap] of symbolStore.entries()) {
        out[sym] = snap;
      }
      cachedWrite(_STORE_KEY, out, TTL.day);
    } catch { /* quota / privacy mode — non-fatal */ }
  }, 500);
}

// ── Merge helpers ────────────────────────────────────────────────────────

/**
 * Per-field shape: which fields are LTP-class (arbitrated by ltp_ts) vs
 * snapshot-class (arbitrated by snapshot_ts). Exchange is treated as
 * snapshot — it rarely changes but should follow the freshest poll.
 */
const _LTP_FIELDS = new Set(['ltp']);

/** Numeric fields we accept; everything else is silently dropped. */
const _NUMERIC_FIELDS = new Set([
  'ltp', 'close', 'open', 'high', 'low',
  'day_change', 'day_change_pct',
  'volume', 'oi', 'bid', 'ask',
]);

/**
 * Merge a single symbol update.
 *
 * @param {string} sym                            — tradingsymbol (case-insensitive)
 * @param {Partial<MarketSnapshot>} fields        — fields to merge; nulls/undef skipped
 * @param {{ ltp_ts?: number, snapshot_ts?: number }} [ts]
 *   ltp_ts:      timestamp tied to the LTP value specifically (SSE: receive ts;
 *                poll: poll-completion ts). Defaults to Date.now().
 *   snapshot_ts: timestamp tied to non-LTP fields. Defaults to Date.now().
 *
 * @returns {boolean} true if any field was written; false if all were
 *                    stale-rejected by ts-arbitration or value-identical.
 */
export function mergeSymbolUpdate(sym, fields, ts = {}) {
  if (!sym || !fields) return false;
  const key = String(sym).toUpperCase();
  const ltp_ts      = Number(ts.ltp_ts      ?? Date.now());
  const snapshot_ts = Number(ts.snapshot_ts ?? Date.now());

  const prev = symbolStore.get(key);
  /** @type {MarketSnapshot} */
  const next = prev
    ? { ...prev }
    : { ltp_ts: 0, snapshot_ts: 0, touched_at: 0 };

  let changed = false;

  for (const [k, raw] of Object.entries(fields)) {
    if (raw == null) continue;

    if (k === 'exchange') {
      if (snapshot_ts >= next.snapshot_ts && raw !== next.exchange) {
        next.exchange = String(raw);
        changed = true;
      }
      continue;
    }

    if (!_NUMERIC_FIELDS.has(k)) continue;
    const v = Number(raw);
    if (!Number.isFinite(v)) continue;

    const isLtp = _LTP_FIELDS.has(k);
    const incomingTs = isLtp ? ltp_ts : snapshot_ts;
    const storedTs   = isLtp ? next.ltp_ts : next.snapshot_ts;

    if (incomingTs < storedTs) continue;  // stale write — reject
    if (/** @type {any} */ (next)[k] === v) continue;  // no-op

    /** @type {any} */ (next)[k] = v;
    changed = true;
  }

  if (!changed) return false;

  // Bump stamps to reflect what was written. Use max() so a partial
  // update (e.g. SSE tick = ltp only) doesn't backslide the snapshot_ts.
  if (fields.ltp != null && Number.isFinite(Number(fields.ltp))) {
    next.ltp_ts = Math.max(next.ltp_ts, ltp_ts);
  }
  const wroteAnySnapshot = Object.keys(fields).some(
    k => (k === 'exchange' || (_NUMERIC_FIELDS.has(k) && !_LTP_FIELDS.has(k)))
  );
  if (wroteAnySnapshot) {
    next.snapshot_ts = Math.max(next.snapshot_ts, snapshot_ts);
  }
  next.touched_at = Math.max(next.ltp_ts, next.snapshot_ts);

  symbolStore.set(key, next);
  _schedulePersist();
  return true;
}

/**
 * Batch variant. Coalesces persist scheduling to one disk write per call
 * batch, so a fetcher writing 300 mover rows pays one timer-arm not 300.
 *
 * @param {Array<{ sym: string, fields: Partial<MarketSnapshot>, ts?: { ltp_ts?: number, snapshot_ts?: number } }>} updates
 * @returns {number} count of updates that wrote at least one field
 */
export function mergeSymbolBatch(updates) {
  if (!Array.isArray(updates) || updates.length === 0) return 0;
  let n = 0;
  for (const u of updates) {
    if (mergeSymbolUpdate(u.sym, u.fields, u.ts)) n++;
  }
  return n;
}

/**
 * Convenience lookup — null if the symbol has never been written.
 * Consumers in BH2 use `$derived(getSnapshot(sym))` to scope re-runs
 * to that sym specifically.
 *
 * @param {string | null | undefined} sym
 * @returns {MarketSnapshot | null}
 */
export function getSnapshot(sym) {
  if (!sym) return null;
  return symbolStore.get(String(sym).toUpperCase()) ?? null;
}

// ── Refresh-cycle reset paths (BH1) ──────────────────────────────────────
//
// Mirrors the backend's runtime_state.set_mode("off"|"soft"|"hard") flow.
// When the operator flips refresh mode from /admin/health:
//
//   soft — backend bypasses cache+DB and re-fetches from broker.
//          Frontend symbolStore drops in-memory entries so the next
//          poll/tick populates from the freshly-fetched broker data.
//          localStorage is kept as a stale-while-revalidate fallback
//          (no UI blank-flash; replaced field-by-field as new data lands).
//
//   hard — soft + backend recycles ticker (TickerManager.recycle()).
//          Frontend additionally wipes localStorage so a page reload
//          doesn't resurrect potentially-bad cached values, AND triggers
//          an SSE restart so the rebuilt backend ticker re-snapshots
//          cleanly into a clean frontend store. Same intent as the
//          ticker recycle: nothing pre-recycle survives.
//
// Both are called from the refreshCycle.applyPersistenceMode() wrapper
// (frontend/src/lib/data/refreshCycle.js) so every surface that flips
// modes gets identical cleanup. Direct callers should not call these
// in isolation — the backend call must succeed first.

/**
 * Soft reset: drop in-memory state; localStorage preserved.
 * @returns {number} count of entries dropped
 */
export function softReset() {
  const n = symbolStore.size;
  symbolStore.clear();
  // Don't cancel the persist timer — if any pending writes were
  // queued before the reset they'll harmlessly write an empty map.
  // We deliberately do NOT touch localStorage here: the operator
  // can still see prior-session values on a page reload if they
  // want a "what did I have before the wipe?" diagnostic.
  return n;
}

/**
 * Hard reset: drop in-memory state AND localStorage.
 * Cancels any pending persist write so we don't immediately
 * re-persist an empty map on top of the freshly-cleared blob.
 * @returns {number} count of entries dropped
 */
export function hardReset() {
  const n = symbolStore.size;
  symbolStore.clear();
  if (_persistTimer != null) {
    clearTimeout(_persistTimer);
    _persistTimer = null;
  }
  _dirty = false;
  if (browser) {
    try {
      // Write an empty object through the cache layer so the next
      // hydrate() pass sees the cleared state immediately (also
      // covers the edge where another tab read mid-clear).
      cachedWrite(_STORE_KEY, {}, TTL.day);
    } catch { /* quota / privacy mode — non-fatal */ }
  }
  return n;
}

/**
 * Diagnostic counts — exposed so a future /admin/health surface can
 * report symbolStore size + age-of-oldest-entry without enumerating
 * every entry on each render.
 */
export function symbolStoreDiagnostics() {
  let oldest = 0;
  for (const snap of symbolStore.values()) {
    const t = snap?.touched_at || 0;
    if (oldest === 0 || (t > 0 && t < oldest)) oldest = t;
  }
  return {
    size: symbolStore.size,
    oldest_touched_at: oldest,
  };
}
