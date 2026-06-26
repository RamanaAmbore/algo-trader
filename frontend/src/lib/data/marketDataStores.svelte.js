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
  batchQuote, fetchSparklines,
} from '$lib/api';
import {
  FO_QUOTE_KEYS, MIDCAP_QUOTE_KEYS, SMLCAP_QUOTE_KEYS,
  INDICES_QUOTE_KEYS, LARGECAP_QUOTE_KEYS, symbolFromQuoteKey,
} from '$lib/data/indexConstituents';
import { mergeSymbolBatch } from './symbolStore.svelte.js';
import { cachedDelete } from './persistentCache.js';
import { browser } from '$app/environment';

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
    batch.push({
      sym,
      fields: {
        ltp:            r.last_price,
        close:          r.previous_close,
        day_change_pct: r.change_pct,
        exchange:       r.exchange,
      },
      ts: { ltp_ts: 0, snapshot_ts: ts },
    });
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
 * Top winners/losers across NSE F&O largecap + midcap + smallcap.
 * Fetcher ports the loadMovers body from MarketPulse so any consumer
 * can subscribe. TTL.short matches the prior 2-min cache window.
 */
export const moversStore = createDataStore({
  key:     'md.movers',
  // TTL.week so the winners/losers panel retains the last in-market
  // snapshot across off-hours instead of going blank on a weekend
  // page reload (operator: "winners and losers data in pulse should
  // be retained after market closure until market reopens"). The
  // marketAwareInterval-driven poll on MarketPulse overwrites with
  // fresh data the moment market reopens.
  ttl:     TTL.week,
  /** @returns {Promise<any[]>} */
  fetcher: async () => {
    const idx = new Set(INDICES_QUOTE_KEYS);
    const lcp = new Set(LARGECAP_QUOTE_KEYS);
    const fo  = new Set(FO_QUOTE_KEYS);
    const mid = new Set(MIDCAP_QUOTE_KEYS);
    const sml = new Set(SMLCAP_QUOTE_KEYS);
    const allKeys = [...new Set([...fo, ...mid, ...sml])];
    const r = await batchQuote(allKeys);
    /** @type {Record<string, any>} */
    const byKey = {};
    for (const it of (r?.items ?? [])) {
      if (!it?.exchange || !it?.tradingsymbol) continue;
      byKey[`${it.exchange}:${it.tradingsymbol}`] = it;
    }
    const _groupFor = (/** @type {string} */ key) =>
      sml.has(key) ? 'smallcap'
    : mid.has(key) ? 'midcap'
    : fo.has(key)  ? 'underlying'
    : null;
    /** @type {any[]} */
    const rows = [];
    for (const [key, it] of Object.entries(byKey)) {
      const group = _groupFor(key);
      if (!group) continue;
      const ltp = Number(it.ltp ?? it.last_price ?? 0);
      let pct = Number(it.change_pct);
      if (!isFinite(pct) || pct === 0) {
        const close = Number(it.close ?? it.previous_close ?? 0);
        if (close > 0 && ltp > 0) pct = ((ltp - close) / close) * 100;
      }
      if (!isFinite(pct) || pct === 0) continue;
      rows.push({
        tradingsymbol: symbolFromQuoteKey(key),
        exchange:      it.exchange,
        last_price:    ltp,
        change_pct:    pct,
        previous_close: Number(it.close ?? it.previous_close ?? 0) || null,
        _moverGroup:   group,
        _isLargeCap:   lcp.has(key) && !mid.has(key) && !sml.has(key),
        _moverDirection: pct >= 0 ? 'winners' : 'losers',
      });
    }
    return rows;
  },
  /** @param {any} r */
  parse:   (r) => {
    const rows = r ?? [];
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
  key:     'md.activeLists',
  ttl:     TTL.minute,
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
        if (res?.data && typeof res.data === 'object') {
          Object.assign(merged, res.data);
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
});
