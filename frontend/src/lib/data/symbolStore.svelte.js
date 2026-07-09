/**
 * symbolStore — symbol-centric reactive store (BH1–BH5 complete).
 *
 * The single source of truth for per-symbol market state across the
 * whole frontend. Every grid + card reads from `getSnapshot(sym)` so
 * pinned, watchlist, positions, holdings, and movers all see the same
 * LTP / day_change / volume / OI for a given symbol — fixing the
 * pinned-shows-zeros asymmetry that motivated the BH migration.
 *
 * Status:
 *   BH1 — shadow dual-write (this file shipped)
 *   BH2 — PositionStrip + MarketPulse pinned read from symbolStore
 *   BH3 — liveLtp writable deleted; SSE writes only here
 *   BH4 — watchQuotesStore deleted; inline /quotes fetch publishes here
 *   BH5 — pulseQuotes contracts published here; mover branch reads here
 *
 * Writers (publishers in marketDataStores.svelte.js):
 *   - publishPositionsRows  ← /api/positions  (poll, ltp_ts=0)
 *   - publishHoldingsRows   ← /api/holdings   (poll, ltp_ts=0)
 *   - publishWatchQuotes    ← /api/watchlist/{id}/quotes (poll, ltp_ts=0)
 *   - publishMoverRows      ← batchQuote(mover universe) (poll, ltp_ts=0)
 *   - publishPulseQuotes    ← batchQuote(in-view) (poll, ltp_ts=0)
 *   - quoteStream.js        ← SSE ticks (ltp_ts=Date.now())
 *
 * Per-field timestamp arbitration
 * ────────────────────────────────
 * SSE ticks (real-time) carry `ltp` only and ALWAYS win for the ltp field;
 * polls carry the whole snapshot but the broker's quote-at-fetch-start is
 * always older than the most recent SSE tick. Two stamps per symbol:
 * `ltp_ts` and `snapshot_ts`. Merges accept new `ltp` only if incoming
 * `ltp_ts >= stored.ltp_ts`, non-LTP fields only if `snapshot_ts >=
 * stored.snapshot_ts`. Polls publish with `ltp_ts=0` so they seed when
 * no SSE has stamped the LTP yet but defer to live ticks otherwise. This
 * kills the SSE-tick-then-late-poll-clobber race the multi-source layout
 * used to have.
 *
 * Persistence
 * ───────────
 * SvelteMap is in-memory. We mirror to localStorage as `{sym: snapshot}`
 * under `rbq.cache.md.symbolStore` so cold mounts paint from cache like
 * positions/holdings already do. Writes are debounced (500ms) to coalesce
 * SSE tick bursts. Prune-on-load drops entries with `touched_at` older
 * than 7 days so closing values survive Fri→Mon dark windows and
 * Diwali-style holiday clusters; anything older with no re-touch is
 * genuinely stale and safe to drop.
 */

import { browser } from '$app/environment';
import { SvelteMap } from 'svelte/reactivity';
import { writable } from 'svelte/store';
import { cachedRead, cachedWrite, TTL } from './persistentCache.js';
import { createTickBus } from './tickFlash.svelte.js';

const _STORE_KEY = 'md.symbolStore';
// 7 days so closing values for actively-traded symbols survive Friday
// close → Monday open (~65 h dark) plus holiday clusters (Diwali week
// can push 5 trading days dark). Operator: "all stats should show
// closing values after market is closed until it reopens." Anything
// older than a week with no re-touch is genuinely stale and safe to
// prune.
const _PRUNE_AGE_MS = 7 * 24 * 60 * 60 * 1000;

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

/**
 * Monotonic write counter — bumps on every mergeSymbolUpdate call that
 * actually wrote at least one field. Consumers that need a "something
 * changed" reactive trigger across the whole map (e.g. MarketPulse's
 * throttle/flash $effect calling ag-Grid refreshCells) subscribe to
 * this writable instead of iterating every entry. Cheaper than
 * `SvelteMap.size` (which only fires on add/remove) and cheaper than
 * scanning the map on every render.
 */
export const symbolTickCount = writable(0);

/**
 * Single-origin directional tick bus — emits {sym, dir, at} on every
 * real LTP write (throttled 250ms per sym). Consumers (RefreshButton,
 * MarketPulse, PositionStrip) subscribe in onMount and tear down in
 * onDestroy. Direction is computed inside _mergeSymbolWrite where
 * prev + next LTP are both in scope — no extra LTP state here.
 *
 * Additive alongside symbolTickCount: symbolTickCount stays for poll-
 * cadence liveness signals (RefreshButton halo, _liveLtpSnap rebuild);
 * tickBus is the per-tick directional signal for flash animations.
 */
export const tickBus = createTickBus({ throttleMs: 250 });

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
      cachedWrite(_STORE_KEY, out, TTL.week);
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
/**
 * Internal per-symbol write. Same arbitration + field semantics as
 * `mergeSymbolUpdate` but does NOT bump `symbolTickCount` and does
 * NOT schedule persist — callers do that once per batch to avoid
 * N fan-outs on the writable subscriber + N persist timer-arms.
 */
function _mergeSymbolWrite(sym, fields, ts = {}) {
  if (!sym || !fields) return false;
  const key = String(sym).toUpperCase();
  const ltp_ts      = Number(ts.ltp_ts      ?? Date.now());
  const snapshot_ts = Number(ts.snapshot_ts ?? Date.now());

  const prev = symbolStore.get(key);
  /** @type {MarketSnapshot} */
  const next = prev
    ? { ...prev }
    : { ltp_ts: 0, snapshot_ts: 0, touched_at: 0 };

  // Capture prev LTP before any writes so _emitTickBus can diff later.
  const _prevLtp = prev?.ltp;

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

    // Price-zero guard (LTP flicker definitive fix Jun 2026):
    //
    // Never write a non-positive value to `ltp` or `close` — full stop.
    // The earlier policy preserved zeros on cold start to let cells
    // paint SOMETHING, but that turned into the operator's flicker
    // root cause: a 0-stamped-fresh SSE snapshot would land first
    // (cold-subscribed but not-yet-traded symbol), claim a fresh
    // ltp_ts via the timestamp arbitration above, and then REJECT
    // every subsequent positive poll (which arrives with ltp_ts=0).
    // The cell would then stay at 0 until another live tick landed,
    // which on an illiquid contract can be minutes.
    //
    // New policy: silently drop the write. The renderer interprets a
    // missing field as "no quote yet" and paints "—" (a far better
    // signal than a misleading 0). A subsequent positive value
    // populates the field cleanly with no ts arbitration loss because
    // there is no stored ts to compare against.
    //
    // Same guard applies to `close` — a zero prev-close corrupts the
    // day_change = (ltp − close) × qty formula into portfolio-value
    // territory (10× to 1000× too large).
    //
    // Belt + suspenders: backend `kite_ticker._on_ticks` and frontend
    // `quoteStream.js` both filter `lp <= 0` before this layer.
    if ((k === 'ltp' || k === 'close') && !(v > 0)) continue;

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

  // Emit to tickBus when LTP actually changed — subscribers drive
  // unified flash animations (RefreshButton, MarketPulse, PositionStrip).
  // Gate: prev must be non-null (first write = baseline, no direction) and
  // the new LTP value must have been written (not stale-rejected or zeroed).
  const _newLtp = next.ltp;
  if (_prevLtp != null && _newLtp != null && _newLtp !== _prevLtp) {
    const dir = _newLtp > _prevLtp ? 'up' : _newLtp < _prevLtp ? 'down' : 'flat';
    tickBus.emit(key, dir);
  }

  return true;
}

export function mergeSymbolUpdate(sym, fields, ts = {}) {
  const wrote = _mergeSymbolWrite(sym, fields, ts);
  if (wrote) _schedulePersist();
  // Always bump symbolTickCount, even on a no-op write (value-identical
  // tick or stale-ts reject). The counter is a "tick arrived" liveness
  // signal — RefreshButton's halo + MarketPulse's _liveLtpSnap rebuild
  // depend on it. Bumping only on value changes left the UI looking
  // dead during heavy flat-market ticks because every tick was a no-op
  // write. Throttles downstream (50ms snap rebuild, 250ms halo + 250ms
  // _liveDeltaByRow) absorb the call rate regardless of write outcome.
  if (sym) symbolTickCount.update(n => n + 1);
  return wrote;
}

/**
 * Batch variant. BH6 fix: coalesces BOTH the persist scheduling AND
 * the `symbolTickCount` writable bump to one each per batch — a
 * fetcher writing 300 mover rows used to fire 300 subscriber callbacks
 * (the flush-timer guard prevented 300 ag-Grid refreshes but didn't
 * stop Svelte's scheduler from running every $effect that subscribed
 * to symbolTickCount 300×).
 *
 * @param {Array<{ sym: string, fields: Partial<MarketSnapshot>, ts?: { ltp_ts?: number, snapshot_ts?: number } }>} updates
 * @returns {number} count of updates that wrote at least one field
 */
export function mergeSymbolBatch(updates) {
  if (!Array.isArray(updates)) return 0;
  let n = 0;
  for (const u of updates) {
    if (_mergeSymbolWrite(u.sym, u.fields, u.ts)) n++;
  }
  if (n > 0) _schedulePersist();
  // Always bump — even on empty batches (which represent "a fetch
  // cycle completed with no rows", still a liveness signal that
  // RefreshButton + MarketPulse should see). One bump per call
  // (not per item) preserves the BH6 anti-saturation fix.
  symbolTickCount.update(c => c + 1);
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
  // Cancel any pending persist write so we don't silently clobber the
  // localStorage blob the operator just kept on purpose. Without this,
  // a write scheduled before softReset fires after the .clear() and
  // serializes the empty map to disk — defeating the stale-while-
  // revalidate intent for the SOFT path. (BH1 audit RISK fix.)
  if (_persistTimer != null) {
    clearTimeout(_persistTimer);
    _persistTimer = null;
  }
  _dirty = false;
  // BH6 fix: bump tickCount so consumers tracking symbolTickCount
  // (MarketPulse's _liveLtpSnap rebuilder, refresh-pulse $effects)
  // see the cleared state immediately. Without this, _liveLtpSnap
  // kept the pre-reset values until the next SSE tick fired —
  // operator-visible as a stale-looking UI after flipping to SOFT.
  symbolTickCount.update(c => c + 1);
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
      cachedWrite(_STORE_KEY, {}, TTL.week);
    } catch { /* quota / privacy mode — non-fatal */ }
  }
  // BH6 fix: bump tickCount (see softReset note).
  symbolTickCount.update(c => c + 1);
  return n;
}
