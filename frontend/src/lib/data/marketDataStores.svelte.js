/**
 * marketDataStores — module-level singletons for the most-used
 * data classes (positions / holdings / funds / movers / activeLists /
 * sparklines). Every consumer imports the same instance, so a load()
 * triggered in PositionStrip is immediately visible in the navbar,
 * dashboard, etc. without any parent-to-child prop threading.
 *
 * BH1–BH5: every fetcher dual-writes to symbolStore via the
 *   publish*Rows / publishWatchQuotes / publishPulseQuotes helpers
 *   below. symbolStore is the single source of truth for per-symbol
 *   market data (ltp / close / day_change / volume / oi / bid / ask);
 *   the section stores keep only what's section-specific (qty / avg /
 *   account / pnl / etc.). watchQuotesStore was deleted in BH4 — the
 *   /watchlist/{id}/quotes fetch lives inline in MarketPulse.loadQuotes
 *   and publishes here directly.
 *
 * The `parse` selectors mirror what each ad-hoc caller was doing inline:
 *   positions/holdings — extract `.rows`, default to []
 *   funds              — extract `.rows`, strip the TOTAL aggregate row
 *                        so callers can sum across accounts without
 *                        double-counting.
 *
 * Parameterised stores (activeListsStore, sparklinesStore) accept args
 * via load(args) — see dataStore.svelte.js for the dedup-by-args contract.
 */

import { createDataStore, TTL } from './dataStore.svelte.js';
import {
  fetchPositions, fetchHoldings, fetchFunds,
  fetchWatchlist,
  fetchMovers,
  batchQuote, fetchSparklines,
} from '$lib/api';
import {
  FO_QUOTE_KEYS, MIDCAP_QUOTE_KEYS, SMLCAP_QUOTE_KEYS,
  INDICES_QUOTE_KEYS, LARGECAP_QUOTE_KEYS, symbolFromQuoteKey,
  FO_UNDERLYINGS, NIFTY_MIDCAP_100, NIFTY_SMLCAP_100,
  FO_STOCK_UNDERLYINGS,
} from '$lib/data/indexConstituents';
import { mergeSymbolBatch } from './symbolStore.svelte.js';
import { cachedDelete } from './persistentCache.js';
import { browser } from '$app/environment';
import { marketAwareInterval, visibleInterval } from '$lib/stores';
// Hardening: dev-only runtime shape assertions on backend responses.
// Vite dead-code-eliminates the assertion body in production so the
// operator's browser pays zero cost.
import { assertMoverRows } from './moverShape.js';
import { assertSparklineResponse } from './sparklineShape.js';

// One-time migration: drop the legacy `md.watchQuotes` localStorage
// blob that BH4 stopped writing (the wrapper store was deleted in
// favour of the inline /watchlist/{id}/quotes fetch + publishWatchQuotes
// → symbolStore). Browsers that ran a pre-BH4 build still carry the
// stale blob until its TTL.week expires — a 7-day window of pre-BH4
// pinned LTPs sitting in storage for no reader. Cheap to drop on
// every module init; no-op once the key is gone.
if (browser) {
  try { cachedDelete('md.watchQuotes'); } catch { /* no-op */ }
}

// ── BH1 dual-write helpers ─────────────────────────────────────────────
//
// Every fetcher that lands rows carrying market-data fields
// (ltp / close / day_change / volume / oi / ...) extracts those fields
// into a per-symbol batch and pushes them into the central symbolStore.
// Section stores still hold the rows themselves for now (BH2 migrates
// consumers; BH3 splits meta from market). Polls use snapshot_ts =
// Date.now() at parse time so newer polls win for snapshot fields.
//
// Polls intentionally publish ltp with ltp_ts = 0 so SSE ticks ALWAYS
// win the LTP slot regardless of who landed last. Without this, a poll
// that completes 30s after the last SSE tick has Date.now() > tick_ts
// and would overwrite the fresh live price with the broker's older
// quote-at-fetch-start. The poll's LTP still seeds the store on the
// VERY first read (when no SSE tick has arrived yet for that symbol),
// because mergeSymbolUpdate accepts incomingTs >= storedTs and the
// initial storedTs = 0. Once any SSE tick has stamped ltp_ts, polls
// stop clobbering it. (BH1 audit CRITICAL fix.)

/**
 * Public publishers — call after a raw broker fetch when bypassing the
 * createDataStore.parse hook (e.g. MarketPulse.loadPulse and
 * PerformancePage.loadAll route through positionsStore.set() / .set() to
 * preserve per-feed error context; that path skips parse and therefore
 * skips the inline publishers below, so callers must invoke these
 * explicitly).
 */
export function publishPositionsRows(rows) { _publishPositionsRows(rows); }
export function publishHoldingsRows(rows)  { _publishHoldingsRows(rows);  }
export function publishMoverRows(rows)     { _publishMoverRows(rows);     }

/** @param {any[]} rows */
function _publishPositionsRows(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return;
  const ts = Date.now();
  /** @type {Array<{sym: string, fields: any, ts: {ltp_ts: number, snapshot_ts: number}}>} */
  const batch = [];
  for (const r of rows) {
    const sym = String(r?.tradingsymbol || r?.symbol || '').toUpperCase();
    if (!sym) continue;
    batch.push({
      sym,
      fields: {
        ltp:        r.last_price,
        close:      r.close_price,
        // BH6 fix: symbolStore.day_change is per-share (matches the
        // semantics of watchQuotes / pulseQuotes / movers which write
        // `q.change` = last_price − close_price). Earlier this wrote
        // r.day_change_val which is portfolio-total (qty × per-share)
        // — a position with qty 100 was overwriting the watchlist's
        // per-share value with a ~100× number, and the watchlist row
        // showed portfolio-₹ as the per-share day_change.
        day_change: r.day_change,
        exchange:   r.exchange,
      },
      ts: { ltp_ts: 0, snapshot_ts: ts },
    });
  }
  mergeSymbolBatch(batch);
}

/** @param {any[]} rows */
function _publishHoldingsRows(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return;
  const ts = Date.now();
  /** @type {Array<{sym: string, fields: any, ts: {ltp_ts: number, snapshot_ts: number}}>} */
  const batch = [];
  for (const r of rows) {
    const sym = String(r?.tradingsymbol || r?.symbol || '').toUpperCase();
    if (!sym) continue;
    batch.push({
      sym,
      fields: {
        ltp:            r.last_price,
        close:          r.close_price,
        // BH6: holdings publisher mirrors positions — symbolStore's
        // day_change slot is per-share, not portfolio-total. Holdings
        // backend exposes both: `day_change` (per-share = ltp − close)
        // and `day_change_val` (portfolio = qty × day_change). Use
        // the per-share value so cross-publisher arbitration is
        // semantically consistent.
        day_change:     r.day_change,
        day_change_pct: r.day_change_percentage,
        exchange:       r.exchange,
      },
      ts: { ltp_ts: 0, snapshot_ts: ts },
    });
  }
  mergeSymbolBatch(batch);
}

/**
 * Publish per-item watchlist quotes (from /watchlist/{id}/quotes) into
 * symbolStore. Exported as `publishWatchQuotes` so MarketPulse can call
 * it directly after the BH4 watchQuotesStore deletion — the wrapper
 * store is gone, but the publish step is still useful when the inline
 * loadQuotes lands.
 *
 * @param {Record<number, any>} byItemId
 */
export function publishWatchQuotes(byItemId) {
  _publishWatchQuotes(byItemId);
}

/** @param {Record<number, any>} byItemId */
function _publishWatchQuotes(byItemId) {
  if (!byItemId || typeof byItemId !== 'object') return;
  const ts = Date.now();
  /** @type {Array<{sym: string, fields: any, ts: {ltp_ts: number, snapshot_ts: number}}>} */
  const batch = [];
  for (const q of Object.values(byItemId)) {
    const sym = String(q?.quote_symbol || q?.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    batch.push({
      sym,
      fields: {
        ltp:            q.ltp,
        close:          q.close,
        open:           q.open,
        day_change:     q.change,
        day_change_pct: q.change_pct,
        volume:         q.volume,
        oi:             q.oi,
        bid:            q.bid,
        ask:            q.ask,
        exchange:       q.exchange,
        // Propagate animation gate so symbolStore snapshots carry the
        // is_animating/price_source fields; mergeWatchlistRows reads
        // them via _propagateStaleAndSource(row, snap).
        is_animating:   q.is_animating,
        price_source:   q.price_source ?? q.ltp_source,
      },
      ts: { ltp_ts: 0, snapshot_ts: ts },
    });
  }
  mergeSymbolBatch(batch);
}

/**
 * Publish the batchQuote(...) items from MarketPulse's loadPulse into
 * symbolStore. These are the broker-quote snapshots for every symbol
 * in view (positions + holdings + watchlist underlyings + contracts);
 * after BH5 they're the authoritative per-poll source for ltp/close/
 * day_change/volume/oi just like watchQuotes and positions/holdings
 * already were.
 *
 * @param {Array<any>} items
 */
export function publishPulseQuotes(items) {
  if (!Array.isArray(items) || items.length === 0) return;
  const ts = Date.now();
  /** @type {Array<{sym: string, fields: any, ts: {ltp_ts: number, snapshot_ts: number}}>} */
  const batch = [];
  for (const q of items) {
    const sym = String(q?.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    batch.push({
      sym,
      fields: {
        ltp:            q.ltp,
        close:          q.close,
        open:           q.open,
        high:           q.high,
        low:            q.low,
        day_change:     q.change,
        day_change_pct: q.change_pct,
        volume:         q.volume,
        oi:             q.oi,
        bid:            q.bid,
        ask:            q.ask,
        exchange:       q.exchange,
      },
      // BH1 audit-CRITICAL pattern: poll ltp_ts=0 so SSE always wins
      // for LTP; poll snapshot_ts=now wins for non-LTP fields.
      ts: { ltp_ts: 0, snapshot_ts: ts },
    });
  }
  mergeSymbolBatch(batch);
}

/** @param {any[]} rows */
function _publishMoverRows(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return;
  const ts = Date.now();
  /** @type {Array<{sym: string, fields: any, ts: {ltp_ts: number, snapshot_ts: number}}>} */
  const batch = [];
  for (const r of rows) {
    const sym = String(r?.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const fields = {
      ltp:            r.last_price,
      close:          r.previous_close,
      day_change_pct: r.change_pct,
      exchange:       r.exchange,
    };
    const tsObj = { ltp_ts: 0, snapshot_ts: ts };
    // MCX mover rows: tradingsymbol is the bare commodity root (CRUDEOIL)
    // but symbolStore + SSE ticks are keyed on the resolved front-month
    // contract (CRUDEOIL26JUNFUT). Write BOTH keys so consumers that read
    // by bare-root (e.g. MarketPulse row-composer via getSnapshot) still
    // get the value, AND the LTP column's mkResolveCellLtp lookup via
    // quote_symbol key also hits.
    const quoteSym = r.quote_symbol ? String(r.quote_symbol).toUpperCase() : null;
    if (quoteSym && quoteSym !== sym) {
      batch.push({ sym: quoteSym, fields, ts: tsObj });
    }
    batch.push({ sym, fields, ts: tsObj });
  }
  mergeSymbolBatch(batch);
}

export { TTL };

/** @typedef {import('./dataStore.svelte.js').createDataStore} DS */

// ── Positions ─────────────────────────────────────────────────────────────

/**
 * Live positions (open + intraday-closed).
 * Keyed to `md.positions` in localStorage (TTL 15 min; aggressively
 * refreshed on mount so the cache is only for instant-paint).
 */
export const positionsStore = createDataStore({
  key:     'md.positions',
  fetcher: fetchPositions,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => {
    const rows = r?.rows ?? [];
    _publishPositionsRows(rows);
    return rows;
  },
});


/**
 * Dedicated positions store for MarketPulse / Pulse page.
 * Isolated from positionsStore so loadPulse's { skipLtp } arg doesn't create
 * a different dedup key in the shared store and race with the cross-page
 * book poller's no-arg load().
 */
export const pulsePositionsStore = createDataStore({
  key:     'md.pulse.positions',
  fetcher: fetchPositions,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => {
    const rows = r?.rows ?? [];
    _publishPositionsRows(rows);
    return rows;
  },
});

/**
 * Dedicated holdings store for MarketPulse / Pulse page.
 * Isolated from holdingsStore for the same reason as pulsePositionsStore.
 */
export const pulseHoldingsStore = createDataStore({
  key:     'md.pulse.holdings',
  fetcher: fetchHoldings,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => {
    const rows = r?.rows ?? [];
    _publishHoldingsRows(rows);
    return rows;
  },
});

// ── Holdings ──────────────────────────────────────────────────────────────

/**
 * Holdings (overnight / long-term book).
 * TTL 15 min — same reasoning as positions.
 */
export const holdingsStore = createDataStore({
  key:     'md.holdings',
  fetcher: fetchHoldings,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => {
    const rows = r?.rows ?? [];
    _publishHoldingsRows(rows);
    return rows;
  },
});

// ── Funds ─────────────────────────────────────────────────────────────────

/**
 * Margin / funds snapshot. The /api/funds response includes a synthetic
 * TOTAL aggregate row so the performance page can display it without
 * an extra summation step. Consumers here (PositionStrip, NavCard) sum
 * per-account rows themselves — strip TOTAL to avoid double-counting.
 */
export const fundsStore = createDataStore({
  key:     'md.funds',
  fetcher: fetchFunds,
  ttl:     TTL.minute,
  /** @param {any} r */
  parse:   (r) => (r?.rows ?? []).filter(
    (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
  ),
});

// ── Movers ────────────────────────────────────────────────────────────────

/**
 * ISO-8601 UTC timestamp of the last snapshot served by the backend,
 * set only when the backend returns a persisted off-hours snapshot
 * (captured_at is non-null in the response). Null during live market
 * hours (data is real-time, no "as of" caveat needed).
 * Read by MarketPulse to show "Last updated: <time>" in the Winners /
 * Losers bucket headers when the market is closed.
 */
let _moversSnapshotAt = $state(/** @type {string | null} */ (null));
export const moversSnapshotAt = {
  get value() { return _moversSnapshotAt; },
};

/**
 * Classify a raw tradingsymbol (no exchange prefix) into ALL mover groups
 * it belongs to. A symbol may belong to multiple tabs simultaneously.
 *
 * Membership rules (non-exclusive):
 * - 'underlying' = any F&O underlying (indices + stocks), including SENSEX/BANKEX
 * - 'large_cap'  = F&O-eligible stocks that are NOT in MIDCAP/SMLCAP 100.
 *   SENSEX/BANKEX are excluded from FO_STOCK_UNDERLYINGS so they never land here.
 * - 'midcap'     = NIFTY MIDCAP 100 constituents (also F&O-eligible stocks)
 * - 'smallcap'   = NIFTY SMLCAP 100 constituents
 *
 * Examples:
 *   RELIANCE → ['underlying', 'large_cap']   (F&O stock, not midcap/smlcap)
 *   BHEL     → ['underlying', 'midcap']      (F&O stock AND NIFTY MIDCAP 100)
 *   SENSEX   → ['underlying']                (BSE index, excluded from large_cap)
 *   UNKNOWN  → []                            (falls to 'underlying' default at store layer)
 *
 * Returns an empty array when the symbol is unknown to all four sets.
 * @param {string} sym
 * @returns {string[]}
 */
function _classifyMoverGroups(sym) {
  const s = String(sym || '').toUpperCase();
  /** @type {string[]} */
  const groups = [];
  if (FO_UNDERLYINGS.has(s))        groups.push('underlying');
  // large_cap = F&O stocks NOT in midcap/smallcap. SENSEX/BANKEX are already
  // excluded from FO_STOCK_UNDERLYINGS so they never enter this branch.
  if (FO_STOCK_UNDERLYINGS.has(s)
      && !NIFTY_MIDCAP_100.has(s)
      && !NIFTY_SMLCAP_100.has(s))  groups.push('large_cap');
  if (NIFTY_MIDCAP_100.has(s))      groups.push('midcap');
  if (NIFTY_SMLCAP_100.has(s))      groups.push('smallcap');
  return groups;
}


/**
 * Top winners/losers fetched from /api/watchlist/movers.
 * The backend endpoint handles persistence: during market hours it
 * fetches live broker quotes and writes a snapshot to DB; when the
 * market is closed it returns the last good snapshot with a
 * `captured_at` field so the frontend can show "Last updated: <time>".
 *
 * keepStaleOnEmpty retains the prior value if the endpoint returns []
 * (instruments cache cold or broker hiccup) so the grids stay
 * populated rather than going blank on a transient error.
 */
export const moversStore = createDataStore({
  key:     'md.movers',
  // TTL.week: the in-memory + localStorage cache retains the last
  // good snapshot across page reloads so the grids paint instantly
  // from cache even on a cold weekend reload before the first API
  // response arrives.
  ttl:     TTL.week,
  keepStaleOnEmpty: true,
  /** @returns {Promise<any[]>} */
  fetcher: async () => {
    const r = await fetchMovers();
    const raw = r?.movers ?? [];
    // Capture the snapshot timestamp (non-null only for off-hours
    // persisted snapshots; null during live market hours).
    _moversSnapshotAt = r?.captured_at ?? null;
    /** @type {any[]} */
    const rows = [];
    for (const it of raw) {
      const sym = String(it?.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const groups = _classifyMoverGroups(sym);
      // Symbols unknown to all four index sets are still shown —
      // the backend may return new F&O additions before the frontend
      // const list is updated. Default them to 'underlying' so they
      // appear in the Underlying tab rather than being silently dropped.
      const effectiveGroups = groups.length > 0 ? groups : ['underlying'];
      const pct = Number(it.change_pct ?? 0);
      rows.push({
        tradingsymbol:   sym,
        exchange:        it.exchange ?? 'NSE',
        last_price:      Number(it.last_price ?? 0),
        change_pct:      pct,
        peak_pct:        Number(it.peak_pct ?? pct),
        previous_close:  Number(it.previous_close ?? 0) || null,
        sticky:          Boolean(it.sticky),
        // Resolved broker/SSE key for MCX rows (e.g. "CRUDEOIL26JUNFUT").
        // Undefined for NSE rows. Used by mkResolveCellLtp and
        // _publishMoverRows to match the symbolStore / _liveLtpSnap key
        // that SSE ticks are written to.
        quote_symbol:    it.quote_symbol ? String(it.quote_symbol).toUpperCase() : undefined,
        _moverGroups:    effectiveGroups,
        _moverGroup:     effectiveGroups[0],   // legacy compat — first membership
        _moverDirection: pct >= 0 ? 'winners' : 'losers',
        // Propagate animation gate from the backend response so
        // _propagateStaleAndSource can suppress animation during
        // closed hours (backend sets is_animating:false for snapshots).
        is_animating:    it.is_animating,
        price_source:    it.price_source ?? it.ltp_source,
      });
    }
    // Hardening: dev-only shape check (Vite strips in prod).
    // Throws with field-attribution if backend payload drifts.
    assertMoverRows(rows);
    return rows;
  },
  /** @param {any} r */
  parse:   (r) => {
    const raw = r ?? [];
    // Schema guard: check raw cached rows BEFORE enrichment. If rows
    // lack both change_pct and _moverDirection they pre-date this schema
    // and _moverGroups/_moverGroup won't be set either. Evict and return
    // [] so the fetcher runs fresh via the enriched path.
    if (raw.length > 0 && raw.some(row => !row.change_pct && !row._moverDirection)) {
      try { localStorage.removeItem('md.movers'); } catch (_) { /* storage unavailable */ }
      return [];
    }
    const rows = raw.map(row => ({
      ...row,
      _moverDirection: row._moverDirection || (Number(row.change_pct ?? 0) >= 0 ? 'winners' : 'losers'),
    }));
    _publishMoverRows(rows);
    return rows;
  },
});

// ── Active watchlists ─────────────────────────────────────────────────────

/**
 * Loaded watchlist items for the currently-selected list ids.
 * Caller passes the ids array as load(ids). Deduped by JSON.stringify
 * so concurrent calls with the same ids share the fetch.
 * TTL.minute mirrors the prior mp.activeLists cache window (slice W).
 *
 * Note: the cache key changed from `mp.activeLists` → `md.activeLists`.
 * That causes a one-time cold miss on the first deploy — subsequent
 * sessions read from the new key and paint instantly.
 */
export const activeListsStore = createDataStore({
  key:              'md.activeLists',
  ttl:              TTL.week,
  keepStaleOnEmpty: true,
  /** @param {number[] | undefined} ids */
  fetcher: async (ids) => {
    if (!ids || ids.length === 0) return [];
    const results = await Promise.all(
      ids.map(id => fetchWatchlist(id).catch(() => null))
    );
    return results.filter(Boolean);
  },
  /** @param {any} r */
  parse:   (r) => r ?? [],
});

// ── Watch quotes (BH4: store deleted) ─────────────────────────────────────
//
// The watchQuotesStore wrapper around fetchWatchlistQuotes(id) was deleted
// in BH4 — its only consumer was MarketPulse buildUnified's wq[it.id]
// lookup, which had been reduced (BH2) to a fallback chain after symbolStore
// and is now gone entirely. Per-list LTP fetches are still useful (they
// seed symbolStore with watchlist-only symbols that don't appear in
// positions/holdings/movers/SSE), but they happen inline in MarketPulse
// via `loadQuotes` → `fetchWatchlistQuotes` → `publishWatchQuotes`. The
// localStorage cache rolls forward to symbolStore's TTL.week blob.

// ── Sparklines ────────────────────────────────────────────────────────────

/**
 * Per-symbol 5-day sparkline arrays (symbol → number[]).
 * Caller passes the current unifiedRows pairs list as load(pairs) so
 * the fetcher can chunk-fetch exactly the symbols in view and prune
 * any symbols that have rotated out (closed positions, removed
 * watchlist items, rolled-over movers).
 *
 * Prune is skipped on the very first call (_firstSparkFetched=false)
 * because unifiedRows may be only partially populated at mount time —
 * a prune on an incomplete universe would drop position sparklines that
 * were just restored from cache. Subsequent 60s-cadence calls always
 * prune against the fully-settled universe.
 *
 * TTL.day matches the backend's daily-eviction contract for past closes.
 */
let _firstSparkFetched = false;

/**
 * Per-symbol stale-better merge — when a fresh sparkline series is
 * either degenerate (≤1 unique value, e.g. backend's `[ltp, ltp]` pad
 * shipped because broker rate-limited the historical_data call) OR
 * shorter than the cached one, keep the cached series instead. This
 * was the root cause of the "sparkline briefly showed the graph, then
 * reset to flat line after reload" symptom — the renderer hydrated
 * from cache with a real curve, then the post-mount loadSparklines
 * round-trip overwrote it with a `[ltp, ltp]` pad for a symbol the
 * broker had just rate-limited.
 *
 * Rules (per symbol):
 *   - fresh non-array / empty → keep cached.
 *   - cached missing → take fresh unconditionally.
 *   - cached has variation (max>min) AND fresh is all-same → keep cached.
 *   - fresh shorter than cached AND fresh has no variation → keep cached.
 *   - otherwise → take fresh.
 *
 * The renderer only needs ≥2 points + variation to look "real". A
 * single-value flat line is a known fallback shape from the backend.
 */
function _hasVariation(arr) {
  if (!Array.isArray(arr) || arr.length < 2) return false;
  const first = arr[0];
  for (let i = 1; i < arr.length; i++) {
    if (arr[i] !== first) return true;
  }
  return false;
}

function _mergeSparkSeries(cached, fresh) {
  if (!Array.isArray(fresh) || fresh.length === 0) return cached;
  if (!Array.isArray(cached) || cached.length === 0) return fresh;
  const freshVar = _hasVariation(fresh);
  const cachedVar = _hasVariation(cached);
  // Strong preference: real curve beats flat line. Discard a fresh
  // degenerate series if the cache holds a real curve.
  if (cachedVar && !freshVar) return cached;
  // Fresh has variation (or cached doesn't) — take fresh regardless of length.
  // The prior length-gate blocked valid closed-hours series that were shorter
  // than the cached version (past+intraday+LTP vs past+intraday only when
  // the market-close LTP tail was missing), causing yesterday's curve to persist.
  return fresh;
}

export const sparklinesStore = createDataStore({
  key:     'md.sparklines',
  ttl:     TTL.day,
  /**
   * @param {{ tradingsymbol: string, exchange: string }[] | undefined} pairs
   */
  fetcher: async (pairs) => {
    if (!pairs?.length) return sparklinesStore.value ?? {};
    const CHUNK = 100;
    /** @type {Record<string, number[]>} */
    let merged = { ...(sparklinesStore.value ?? {}) };
    for (let i = 0; i < pairs.length; i += CHUNK) {
      const slice = pairs.slice(i, i + CHUNK);
      try {
        const res = await fetchSparklines(slice, 5);
        // Hardening: dev-only shape check (Vite strips in prod).
        // Throws with field-attribution if backend response drifts
        // (e.g. .data becomes array, series carries NaN, refreshed_at drops).
        assertSparklineResponse(res);
        if (res?.data && typeof res.data === 'object') {
          // Per-symbol stale-better merge instead of blind Object.assign.
          // See _mergeSparkSeries doc above for the rule set.
          for (const [sym, fresh] of Object.entries(res.data)) {
            const cached = merged[sym];
            const next = _mergeSparkSeries(cached, /** @type {any} */ (fresh));
            if (next !== undefined) merged[sym] = /** @type {any} */ (next);
          }
        }
      } catch (_) { /* non-fatal — keep whatever we have */ }
    }
    // Active-universe prune (slice T fix). Drop symbols no longer in
    // the current pairs set so the cache doesn't grow unbounded as
    // movers rotate. Skipped on the first call to avoid pruning
    // sparklines restored from cache when unifiedRows is still partial.
    if (_firstSparkFetched) {
      const active = new Set(pairs.map(p => p.tradingsymbol));
      for (const sym of Object.keys(merged)) {
        if (!active.has(sym)) delete merged[sym];
      }
    }
    _firstSparkFetched = true;
    return merged;
  },
  /** @param {any} r */
  parse: (r) => r ?? {},
  // Stale-while-valid guard — if a sparkline fetch comes back with
  // no symbols at all (broker outage, network drop), don't overwrite
  // the populated map. Per-symbol degenerate-fresh handling lives in
  // _mergeSparkSeries above; this catches the wholesale-empty case.
  keepStaleOnEmpty: true,
});

// ── Cross-page book poller (operator-approved final design 2026-06-28) ────
//
// Operator's stated end-state: "every page should poll when viewport active.
// only when viewport is not active for 5 mins, go into hibernation."
//
// Before this slice each page that displayed positions / holdings / funds
// (re-)mounted its own poll cycle. Switching from /pulse (5 s loadPulse) to
// /dashboard (15 s loadHero) meant positions data went up to 15 s stale on
// nav, and pages paid a mount-fetch latency on every visit.
//
// Now a SINGLE layout-resident poll cycle runs at the unified `pulse.tick_
// interval_ms` cadence (default 5 s) regardless of which page is routed.
// Pages stay as consumers: they read `positionsStore.value` /
// `holdingsStore.value` / `fundsStore.value`, and the data is already hot
// on first paint. Cross-page nav becomes instant.
//
// `marketAwareInterval` adds two gates for free:
//   1. Market-hours gate — no broker call outside NSE/MCX session.
//   2. Visibility hibernation — after `polling.idle_timeout_min` (default
//      5 min) hidden, the inner `visibleInterval` switches to throttle
//      mode (here 30 s) and on tab-return fires within ~200 ms (refire
//      contract). Tab visible OR hidden < threshold → normal 5 s cadence.
//
// In-flight dedup inside createDataStore means a stray page-mounted
// `.load()` call landing concurrently with this poller's tick shares one
// HTTP round-trip — backwards-compatible with surfaces that still call
// `positionsStore.load()` on mount for the kick-off fetch (PositionStrip,
// dashboard loadHero, MarketPulse loadPulse — all dedupe transparently).
//
// `started` flag makes the call idempotent: the (algo) layout invokes
// `startBookPollers()` in onMount; if another component also calls it,
// the second call is a no-op.
let _bookPollerStarted = false;
/** @type {(() => void) | null} */
let _bookPollerTeardown = null;

/**
 * Reactive counter incremented after every successful book-poller cycle.
 * PositionStrip watches this to fire its poll-stamp and flash animation
 * at the book-poller cadence (default 5 s) instead of its own 30 s interval.
 */
let _bookPollerTick = $state(0);
export const bookPollerTick = { get value() { return _bookPollerTick; } };

/** Throttle (hidden / hibernation) cadence in ms. Critical book data —
 *  keep a slow heartbeat alive across a long backgrounded window so the
 *  operator returns to current numbers without a cold-start cycle. */
const _BOOK_HIDDEN_MS = 30_000;

/** Default foreground cadence (overridden by `pulse.tick_interval_ms`
 *  setting on the layout's onMount). 5 s matches the prior MarketPulse
 *  cadence so cross-page hotness preserves the previous "live feel". */
let _bookForegroundMs = 5_000;

async function _tickBookPollers() {
  // Promise.allSettled so a single broker failure (e.g. /api/funds
  // 502 mid-session) doesn't stall the next tick's positions refresh.
  // The stores themselves keep last-good value on error — no UI flash.
  try {
    await Promise.allSettled([
      positionsStore.load(),
      holdingsStore.load(),
      fundsStore.load(),
    ]);
    // Signal completion so PositionStrip's flash animation fires at
    // book-poller cadence (default 5 s) rather than its own 30 s interval.
    _bookPollerTick++;
  } catch (_) { /* defensive — allSettled should never throw, but guard */ }
}

/**
 * Start the cross-page book poller. Idempotent — call from any layout/
 * route. The (algo) layout owns the single invocation today; SSR / non-
 * browser contexts are no-ops.
 *
 * @param {number} [intervalMs]  Foreground cadence (default 5 s). Use the
 *   `pulse.tick_interval_ms` setting value to align with the prior
 *   MarketPulse loop.
 */
export function startBookPollers(intervalMs) {
  if (!browser || _bookPollerStarted) return;
  _bookPollerStarted = true;
  if (Number.isFinite(intervalMs) && /** @type {number} */ (intervalMs) > 0) {
    _bookForegroundMs = /** @type {number} */ (intervalMs);
  }
  // Kick once immediately so the first paint after a cold load doesn't
  // wait `intervalMs` for the initial fetch. createDataStore already
  // dedups against any page-mounted `.load()` racing this on the same
  // tick.
  _tickBookPollers();
  // visibleInterval fires regardless of market hours so the book poller
  // stays alive during premarket / closed hours. Stores return snapshot
  // data cheaply (no broker calls when closed). hiddenMs throttle
  // applies only when the tab is backgrounded (hibernation). The
  // market-hours gate was removed because NavStrip values must stay
  // current during premarket (operator watching the strip before open).
  _bookPollerTeardown = visibleInterval(_tickBookPollers, _bookForegroundMs, `throttle:${_BOOK_HIDDEN_MS}`);
}

/** Update the foreground cadence at runtime. Used by the layout when the
 *  `pulse.tick_interval_ms` setting changes — restarts the underlying
 *  timer with the new cadence; idempotent on identical input. */
export function setBookPollerInterval(intervalMs) {
  if (!browser || !Number.isFinite(intervalMs) || /** @type {number} */ (intervalMs) <= 0) return;
  if (intervalMs === _bookForegroundMs) return;
  _bookForegroundMs = /** @type {number} */ (intervalMs);
  if (_bookPollerStarted && _bookPollerTeardown) {
    _bookPollerTeardown();
    _bookPollerTeardown = visibleInterval(_tickBookPollers, _bookForegroundMs, `throttle:${_BOOK_HIDDEN_MS}`);
  }
}

/** Teardown — exposed for tests + dev hot-reload. Production layout
 *  doesn't unmount, so this is rarely called outside the test harness. */
export function stopBookPollers() {
  if (_bookPollerTeardown) { _bookPollerTeardown(); _bookPollerTeardown = null; }
  _bookPollerStarted = false;
}
