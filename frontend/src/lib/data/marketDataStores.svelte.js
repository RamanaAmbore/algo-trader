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
  fetchOrders, fetchAlgoOrdersRecent,
} from '$lib/api';
import { isCurrentTradingSession } from '$lib/dateFormat.js';
import { noteOrderPollFills } from './orderFillDetector.js';
import {
  FO_QUOTE_KEYS, MIDCAP_QUOTE_KEYS, SMLCAP_QUOTE_KEYS,
  INDICES_QUOTE_KEYS, LARGECAP_QUOTE_KEYS, symbolFromQuoteKey,
  FO_UNDERLYINGS, NIFTY_MIDCAP_100, NIFTY_SMLCAP_100,
  FO_STOCK_UNDERLYINGS,
} from '$lib/data/indexConstituents';
import { mergeSymbolBatch } from './symbolStore.svelte.js';
import { cachedDelete } from './persistentCache.js';
import { browser } from '$app/environment';
import { marketAwareInterval, visibleInterval, lastRefreshAt } from '$lib/stores';
import { isNseOpen, isMcxOpen, isBookSnapshotGraceWindow } from '$lib/marketHours';
import { bookChanged } from '$lib/data/bookChanged.js';
import { subscribeSkipInitial } from './subscribeSkipInitial.js';
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
        // `prev_close` rename (commit 933a9a88) — PositionRow no longer
        // carries `close_price`; the frozen prior-session settlement
        // price (daily_book.ltp basis) lives in `prev_close` now. See
        // backend/api/schemas.py:PositionRow.prev_close.
        close:      r.prev_close,
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
        // `prev_close` rename (commit 933a9a88) — HoldingRow no longer
        // carries `close_price`; see backend/api/schemas.py:HoldingRow.prev_close.
        close:          r.prev_close,
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
 * `close` is special-cased: `_publishPositionsRows` is the sole
 * authority for `close` on symbols that are currently-held positions
 * (it writes the correct `daily_book.ltp`-basis `prev_close`; this
 * quote path carries Kite's banned `ohlc.close`). Both publishers
 * stamp `snapshot_ts: Date.now()` with no inherent ordering, so
 * whichever call lands last used to win — a held position's `close`
 * could silently flip to the wrong basis. Callers pass
 * `opts.skipCloseSymbols` (a Set of uppercased tradingsymbols — the
 * caller's currently-held positions with a valid `prev_close`) so this
 * publisher omits `close` entirely for those symbols, leaving the
 * positions publisher as sole authority. Non-held symbols (underlying/
 * root symbols with no position) are unaffected — `close` keeps
 * writing from this quote path, the documented legitimate fallback use.
 *
 * @param {Array<any>} items
 * @param {{ skipCloseSymbols?: Set<string> }} [opts]
 */
export function publishPulseQuotes(items, opts) {
  if (!Array.isArray(items) || items.length === 0) return;
  const skipClose = opts?.skipCloseSymbols instanceof Set ? opts.skipCloseSymbols : null;
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
        // Omitted (not set to undefined-key) for held positions — see
        // skipCloseSymbols doc above. _mergeSymbolWrite already skips
        // `raw == null` fields, so `undefined` here is a safe no-op.
        close:          (skipClose && skipClose.has(sym)) ? undefined : q.close,
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

// ── Degraded-fetch meta extractor (real-money guard, 2026-09) ──────────────
//
// positions/holdings/funds responses (PositionsResponse / HoldingsResponse /
// FundsResponse — backend/api/schemas.py) all carry the same two fields:
// `stale_accounts: string[]` (accounts substituted from broker_apis'
// last-known-good frame cache because their circuit breaker was open at
// fetch time) and `as_of` (non-null only for a persisted off-hours
// snapshot). Shared extractor so all five book stores below stay
// consistent — see dataStore.svelte.js's extractStaleMeta/createDataStore
// `meta` option doc for how this is consumed.
/** @param {any} r */
function _bookStaleMeta(r) {
  return { staleAccounts: r?.stale_accounts ?? [], asOf: r?.as_of ?? null };
}

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
  meta:    _bookStaleMeta,
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
  meta:    _bookStaleMeta,
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
  meta:    _bookStaleMeta,
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
  meta:    _bookStaleMeta,
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
  meta:    _bookStaleMeta,
  /** @param {any} r */
  parse:   (r) => (r?.rows ?? []).filter(
    (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
  ),
});

// ── Orders ────────────────────────────────────────────────────────────────
//
// Order book SWR extension (PLAN.md Part B, item B1). The actual blank-
// on-reload surface reported was OrderBook.svelte's OWN `orderRows`/
// `_loading` local state (shown as "Loading…" until its first fetch
// resolved) — NOT the /orders page's own `orders = $state([])`, which is
// nearly dead code. This promotes the merge logic OrderBook.svelte used
// to own locally — fetchOrders()+fetchAlgoOrdersRecent() dedup, chase
// cancel-and-replace supersede tracking, and the session-boundary filter
// — to this shared createDataStore singleton, so every mount of
// OrderBook.svelte (across /orders, SymbolPanel embeds, etc.) shares one
// poll and paints last-good rows immediately on reload instead of
// "Loading…". LogPanel.svelte keeps its own INDEPENDENT order merge for
// its Orders/Console tabs (mode/simScope-branching fetchers don't fit
// createDataStore's key-stable, must-throw contract cleanly) — see that
// file's own Tier-2 hydrate-and-persist pair (PLAN.md B2) for its
// treatment of the identical reload-blank problem. `HeldOrdersCard.svelte`
// and `ChaseCard.svelte` are deliberately OUT of scope for this store —
// lower-traffic surfaces, not the reported pain point.

/** Mirrors OrderBook.svelte's own `_STATUS_PREDICATES` table (open/held/
 *  complete only — the three needed by the merge/session-filter below).
 *  Duplicated, not imported, because OrderBook.svelte's own copy also
 *  drives unrelated DISPLAY-filter chips (chase/rejected_cancelled) that
 *  have nothing to do with the fetch-level merge here. Same existing
 *  duplication precedent as LogPanel.svelte's independent copy of the
 *  identical table — see
 *  frontend/src/lib/__tests__/orderStatusPredicates.test.js. Keep these
 *  three in sync with OrderBook.svelte's `.open`/`.held`/`.complete`. */
function _ordersIsOpenStatus(/** @type {string} */ st) {
  return st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING';
}
function _ordersIsHeldStatus(/** @type {string} */ st) {
  return st === 'HELD' || st === 'CANCEL_FAILED';
}
function _ordersIsCompleteStatus(/** @type {string} */ st) {
  return st === 'COMPLETE' || st === 'FILLED';
}

/** Row timestamp → epoch-ms — ported verbatim from OrderBook.svelte's
 *  pre-B1 `_rowTsMs` (see that file's history for the Safari Date.parse
 *  non-ISO-string rationale). */
function _ordersRowTsMs(/** @type {any} */ o) {
  if (o?.order_timestamp) {
    const s = String(o.order_timestamp).trim();
    const m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
    return m ? Date.parse(`${m[1]}T${m[2]}+05:30`) : Date.parse(s);
  }
  if (o?.created_at) {
    const s = String(o.created_at).trim();
    if (!s) return NaN;
    return /[Zz]|[+-]\d{2}:\d{2}$/.test(s) ? Date.parse(s) : Date.parse(`${s}Z`);
  }
  return NaN;
}

/** Drops TERMINAL rows from a PRIOR trading session (08:00 IST rollover) —
 *  ported verbatim from OrderBook.svelte's pre-B1 `_isCurrentSessionRow`.
 *  Still-working (OPEN/TRIGGER PENDING) and HELD/CANCEL_FAILED rows are
 *  NEVER dropped regardless of age — operator instruction (2026-09-30):
 *  "keep them visible until reconciled." */
function _ordersIsCurrentSessionRow(/** @type {any} */ o) {
  const st = (o?.status || '').toUpperCase();
  if (_ordersIsOpenStatus(st)) return true;
  if (_ordersIsHeldStatus(st)) return true;
  const ms = _ordersRowTsMs(o);
  if (!Number.isFinite(ms)) return true; // can't judge — keep, don't hide data we can't classify
  return isCurrentTradingSession(ms);
}

// ── Chase cancel-and-replace dedup (ported verbatim from OrderBook.svelte's
// pre-B1 2026-10-23 fix — see that file's git history for the full race
// rationale: during a chase cancel-and-replace, the algo row moves on to a
// NEW broker_order_id immediately, but the broker's own order book can lag
// behind the cancel by a second or more, producing a confusing duplicate
// "open" card for the just-superseded order). Module-level (not $state) —
// this is fetch-cycle bookkeeping, never rendered directly. Now shared
// across every OrderBook.svelte mount instead of per-component-instance,
// which is strictly more correct: there is only one real fetch cycle now. */
let _ordersPrevAlgoBrokerIdById = new Map();
/** @type {Map<string, {algoOrderId: any, ts: number}>} */
let _ordersSupersededBrokerIds = new Map();
const _ORDERS_SUPERSEDED_GRACE_MS = 25_000;
let _ordersBackendSupersededShipped = false;
/** @type {Set<string>} */
let _ordersBackendSupersededIds = new Set();

/**
 * Predicate the merge filter below calls to decide which broker_order_id
 * counts as superseded — ported verbatim from OrderBook.svelte's pre-B1
 * `_isSupersededBrokerId` (including that function's own doc comment,
 * which claimed OrderBook.svelte's Cancel-button guard also called it;
 * checked against the real pre-B1 code and that claim was already
 * inaccurate THEN — `_cancelRow`'s stale-broker-id guard has always done
 * its own independent, fresh re-fetch instead, precisely because this
 * predicate only ever reflects the LAST POLL's snapshot, not "right now"
 * — see that function's own comment in OrderBook.svelte). Exported so a
 * future consumer of this module's merge semantics has one place to read
 * it from; not currently called from outside `_fetchOrdersMerged` itself.
 * @param {string} bid
 */
export function isOrderSuperseded(bid) {
  if (_ordersBackendSupersededShipped) return _ordersBackendSupersededIds.has(bid);
  return _ordersSupersededBrokerIds.has(bid);
}

/**
 * Fetch + merge the broker order book with the algo order book — ported
 * verbatim from OrderBook.svelte's pre-B1 `_loadOrders` (see that file's
 * git history for every individual fix's own rationale: Sprint 2a's
 * broker_order_id dedup key, the chase-field passthrough, the
 * hold_json passthrough, etc.).
 *
 * Fetcher-must-throw contract (createDataStore's keep-last-good path) —
 * OrderBook.svelte's OWN pre-B1 code used `Promise.allSettled` and
 * silently returned early (freezing `orderRows` by hand) when the broker
 * leg rejected. That was a second, parallel freeze mechanism duplicating
 * what `createDataStore`'s `_fetch` catch handler already does for every
 * other store in this module. A rejected broker leg now THROWS, so
 * `_value` stays at last-good and `_error`/`meta.fetchFailed` are set by
 * the shared mechanism instead. A rejected ALGO leg does NOT throw —
 * `algoRows` degrades to `[]`, matching the pre-B1 tolerance (the algo
 * book is a secondary/enrichment source here, not the authoritative one).
 */
async function _fetchOrdersMerged() {
  const [brokerResp, algoResp] = await Promise.allSettled([
    fetchOrders(),
    fetchAlgoOrdersRecent(100, 'all'),
  ]);
  if (brokerResp.status !== 'fulfilled') {
    throw (brokerResp.reason instanceof Error
      ? brokerResp.reason
      : new Error('Failed to fetch orders'));
  }
  const brokerRows = Array.isArray(brokerResp.value?.rows) ? brokerResp.value.rows : [];
  const algoRows = (algoResp.status === 'fulfilled' && Array.isArray(algoResp.value))
    ? algoResp.value
    : [];

  // Chase-field passthrough — a broker-fetched row never carries chase
  // bookkeeping (attempts/current_limit/next_attempt_at/hold_json/
  // agent_origin live only on the AlgoOrder row).
  const _algoByBrokerId = new Map();
  for (const row of algoRows) {
    const bid = String(row?.broker_order_id || '');
    if (bid) _algoByBrokerId.set(bid, row);
  }

  // Recomputed every fetch so a field that's absent one poll and present
  // the next (e.g. a deploy landing mid-session) is picked up immediately.
  _ordersBackendSupersededShipped = algoRows.some(r => r && Object.prototype.hasOwnProperty.call(r, 'superseded_broker_order_ids'));
  _ordersBackendSupersededIds = new Set(
    algoRows.flatMap(r => Array.isArray(r?.superseded_broker_order_ids)
      ? r.superseded_broker_order_ids.map(String)
      : [])
  );

  // Chase cancel-and-replace dedup — gated on a genuinely fresh algo poll;
  // a rejected algoResp (network blip) must not overwrite the previous-ids
  // history, which would blind the very next poll to a supersede that's
  // still mid-flight.
  if (algoResp.status === 'fulfilled') {
    const _now = Date.now();
    for (const [bid, info] of _ordersSupersededBrokerIds) {
      if (_now - info.ts > _ORDERS_SUPERSEDED_GRACE_MS) _ordersSupersededBrokerIds.delete(bid);
    }
    for (const row of algoRows) {
      const rid = row?.id;
      if (rid == null) continue;
      const prevBid = _ordersPrevAlgoBrokerIdById.get(rid);
      const curBid = String(row?.broker_order_id || '');
      if (prevBid && curBid && prevBid !== curBid) {
        _ordersSupersededBrokerIds.set(prevBid, { algoOrderId: rid, ts: _now });
      }
    }
    const _nextAlgoSnapshot = new Map();
    for (const row of algoRows) {
      if (row?.id != null) _nextAlgoSnapshot.set(row.id, String(row?.broker_order_id || ''));
    }
    _ordersPrevAlgoBrokerIdById = _nextAlgoSnapshot;
  }

  const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
  const algoOnly = algoRows.filter(o => {
    const bid = String(o?.broker_order_id || '');
    return !bid || !brokerIds.has(bid);
  });

  // Drop a just-superseded broker row UNLESS it's already terminal-filled
  // (a late fill racing the cancel must still render and still feed
  // `noteOrderPollFills` below).
  const brokerRowsVisible = brokerRows.filter(o => {
    const oid = String(o?.order_id || '');
    if (!isOrderSuperseded(oid)) return true;
    return _ordersIsCompleteStatus((o?.status || '').toUpperCase());
  });

  const merged = [
    ...brokerRowsVisible.map(o => {
      const _m = _algoByBrokerId.get(String(o?.order_id || ''));
      return _m
        ? { ...o, _rowOrigin: 'broker', attempts: _m.attempts, current_limit: _m.current_limit, next_attempt_at: _m.next_attempt_at, _algoId: _m.id, agent_origin: _m.agent_origin, hold_json: _m.hold_json }
        : { ...o, _rowOrigin: 'broker' };
    }),
    ...algoOnly.map(o => ({ ...o, _rowOrigin: 'algo' })),
  ].filter(_ordersIsCurrentSessionRow);

  merged.sort((a, b) => (_ordersRowTsMs(b) || 0) - (_ordersRowTsMs(a) || 0));

  // Channel-agnostic fresh-books trigger — see orderFillDetector.js
  // header. Shared module-level Map, safe to call once per consolidated
  // fetch cycle instead of once per component instance.
  noteOrderPollFills(merged);

  return merged;
}

/**
 * Merged broker + algo order book (OrderBook.svelte's grid). TTL.minute
 * mirrors positionsStore's own Tier-2 localStorage window — the actual
 * freshness guarantee comes from the ~5s poll cadence; the cache exists
 * only so a reload paints the last-good book instantly instead of
 * "Loading…".
 */
export const ordersStore = createDataStore({
  key:     'md.orders',
  fetcher: _fetchOrdersMerged,
  ttl:     TTL.minute,
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

/** Unsubscribe handle for the `bookChanged` WS-event subscription below
 *  (one-shot forced refresh on a real backend write — e.g. a closed-
 *  hours broker-account re-enable). Mirrors `_bookPollerTeardown`'s
 *  module-scoped start/stop convention. */
/** @type {(() => void) | null} */
let _bookChangedUnsub = null;

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
let _bookLiveMs   = 5_000;
let _bookClosedMs = 30 * 60 * 1_000;

// Timestamp (ms) of the book poller's last successful fetch made while
// NOT live-trading (a grace-window fetch — see isBookSnapshotGraceWindow).
// Throttles grace-window fetches to `_bookClosedMs` apart even though the
// outer timer itself keeps ticking at live cadence (the tick-rate check is
// free; only the actual network round-trip is throttled). Reset to 0 on
// every genuinely live fetch so a fresh closed-window throttle window
// starts the next time the market closes.
let _lastGraceFetchAt = 0;

// Promise.allSettled so a single broker failure (e.g. /api/funds 502
// mid-session) doesn't stall the next tick's positions refresh. The
// stores themselves keep last-good value on error — no UI flash.
//
// `force` (default false) — bypass createDataStore's in-flight dedup so
// a WS-triggered one-shot refresh (see the `bookChanged` subscription in
// startBookPollers below) always issues a brand-new fetch rather than
// sharing whatever request (routine poller tick, mount kick) may already
// be in flight with the same (empty) args — that in-flight request could
// have been issued BEFORE the backend wrote the fresh row this refresh
// exists to pick up, and createDataStore's own generation guard (see
// dataStore.svelte.js) would then apply the older response.
async function _loadBookOnce(force = false) {
  try {
    const opts = force ? { force: true } : undefined;
    await Promise.allSettled([
      positionsStore.load(opts),
      holdingsStore.load(opts),
      pulsePositionsStore.load(opts),
      pulseHoldingsStore.load(opts),
      fundsStore.load(opts),
    ]);
    // Signal completion so PositionStrip's flash animation fires at
    // book-poller cadence (default 5 s) rather than its own 30 s interval.
    _bookPollerTick++;
    // Freeze (not reset) the header "last refreshed" clock during closed
    // market hours — a grace-window fetch (08:00 reset / 23:45 MCX
    // settlement write) still re-serves the frozen `daily_book` snapshot
    // to the UI, so it must not advance the clock either. Same AND-gate
    // shape as RefreshButton.svelte / tickStaleness.svelte.js.
    const live = isNseOpen() || isMcxOpen();
    if (live) lastRefreshAt.set(Date.now());
    // A forced refresh (the bookChanged WS subscription below) is a
    // one-off, event-triggered fetch unrelated to the routine grace-
    // window throttle — it must not stamp or reset _lastGraceFetchAt:
    // stamping "now" would suppress the next genuine grace-window fetch,
    // and resetting to 0 would make the next one fire immediately
    // regardless of the throttle. Leave that bookkeeping untouched.
    if (!force) _lastGraceFetchAt = live ? 0 : Date.now();
    // Keep the running timer's cadence reconciled with the `polling.
    // book_live_ms` setting (settings change takes effect on the next
    // landed fetch). No more open/closed branch here — the closed-window
    // cadence decision is made by the fetch-skip logic in
    // _tickBookPollers below, not by re-arming the timer itself.
    setBookPollerInterval(_bookLiveMs);
  } catch (_) { /* defensive — allSettled should never throw, but guard */ }
}

async function _tickBookPollers() {
  // Fully closed (no NSE, no MCX) AND outside the two narrow snapshot-
  // write grace windows (08:00 daily reset / MCX 23:45 settlement write,
  // see isBookSnapshotGraceWindow's doc) — nothing in `daily_book` can
  // change until one of those windows opens, so skip the fetch entirely.
  // isNseOpen/isMcxOpen/isBookSnapshotGraceWindow are free, synchronous,
  // already-holiday-aware reads of the `_serverStatus` cache kept warm by
  // the existing, separate startMarketStatusPoller() (stores.js, 5-min
  // cadence) — no network call either way. _bookPollerTick / lastRefreshAt
  // deliberately do NOT advance on a skipped tick — the data hasn't
  // changed, so there is nothing new to signal.
  if (!isNseOpen() && !isMcxOpen()) {
    if (!isBookSnapshotGraceWindow()) return;
    // Inside a grace window: throttle the actual fetch to `_bookClosedMs`
    // apart rather than firing on every live-cadence tick — the timer
    // itself keeps running at live cadence (cheap, no network); only the
    // round-trip is throttled.
    if (Date.now() - _lastGraceFetchAt < _bookClosedMs) return;
  }
  await _loadBookOnce();
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
    _bookLiveMs = /** @type {number} */ (intervalMs);
  }
  // Kick once immediately, UNGATED, so the first paint after a cold load
  // (including a page load during the fully-closed window — overnight, a
  // weekend, a holiday cluster) always gets the last-known-good snapshot
  // rather than nothing. This is a single one-time fetch per mount, not a
  // recurring one, so it doesn't reintroduce the pointless-polling problem
  // this change fixes. createDataStore already dedups against any
  // page-mounted `.load()` racing this on the same tick.
  _loadBookOnce();
  // visibleInterval fires regardless of market hours — it's the RECURRING
  // tick that _tickBookPollers gates (skip fetch when fully closed and
  // outside a snapshot-write grace window, see above). hiddenMs throttle
  // applies only when the tab is backgrounded (hibernation).
  _bookPollerTeardown = visibleInterval(_tickBookPollers, _bookForegroundMs, `throttle:${_BOOK_HIDDEN_MS}`);

  // One-shot forced refresh on a real `book_changed` WS event — e.g. a
  // closed-hours broker-account re-enable writing a fresh `daily_book`
  // row (backend/api/routes/brokers.py:_snapshot_books_if_closed). That
  // write happens once, from whichever tab/role performed the re-enable,
  // but every OTHER open tab/device/role must also see the refreshed
  // data — _tickBookPollers' own closed-hours gate (above) otherwise
  // skips every poll until the next grace window or market open.
  //
  // subscribeSkipInitial drops the synchronous initial-value echo
  // svelte/store always fires on subscribe (bookChanged may already be
  // >0 by the time a later tab mounts) — without it every page mount
  // would fire a spurious forced refresh. Every REAL subsequent
  // increment (the WS bus already coalesces a burst of events into one
  // increment — see bookChanged.js's debounce) triggers exactly one
  // forced _loadBookOnce(true) here: edge-triggered on an actual
  // backend event, never a resumed/continuous poll.
  //
  // Market-hours guard (2026-11 follow-up, operator-confirmed): bookChanged
  // also bumps on fill_event/positions_refreshed, and book_changed itself
  // fires on EVERY terminal order status (including each chase cancel-
  // and-replace's CANCELLED postback) — during live trading hours that's
  // a forced 5-store refresh per fill/cancel-replace in every open tab,
  // pure redundant broker-layer load on top of the already-running 5 s
  // poller, which already covers this. _snapshot_books_if_closed (the
  // backend write this subscription exists to pick up) is itself a
  // no-op while the market is open, so there's nothing new to fetch
  // either way. Same condition _tickBookPollers already uses to decide
  // "nothing in daily_book can change right now" — when the market is
  // open, no-op entirely (not even the unforced path; the routine
  // poller already has it covered).
  _bookChangedUnsub = subscribeSkipInitial(bookChanged, () => {
    if (isNseOpen() || isMcxOpen()) return;
    _loadBookOnce(true);
  });
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

export function setBookPollerLiveMs(ms) {
  if (!browser || !Number.isFinite(ms) || ms < 1000) return;
  _bookLiveMs = ms;
}

export function setBookPollerClosedMs(ms) {
  if (!browser || !Number.isFinite(ms) || ms < 60000) return;
  _bookClosedMs = ms;
}

/** Teardown — exposed for tests + dev hot-reload. Production layout
 *  doesn't unmount, so this is rarely called outside the test harness. */
export function stopBookPollers() {
  if (_bookPollerTeardown) { _bookPollerTeardown(); _bookPollerTeardown = null; }
  if (_bookChangedUnsub) { _bookChangedUnsub(); _bookChangedUnsub = null; }
  _bookPollerStarted = false;
}

// ── Mount-independent fill-watch backstop cadence (Fix 5, 2026-10) ──────
//
// `pollOrderFillWatch` (orderFillPoller.js) is a pure function with no
// owned interval — the actual `visibleInterval(pollOrderFillWatch, 5000)`
// call lives in `(algo)/+layout.svelte` (outside this module's file
// scope for this change). Since this poller is now documented as a pure
// safety-net backstop for non-WS-broadcasting channels (position_filled
// WS events + OrderBook/LogPanel's own poll loops are the primary fill-
// detection path per CLAUDE.md), its intended default cadence is 60 s
// (SLOW), not the current 5 s.
//
// This getter/setter pair mirrors `setBookPollerLiveMs`/
// `setBookPollerClosedMs` exactly so a FUTURE settings-driven wiring
// (operator ask: source from a `polling.slow_ms` registry setting, the
// SAME fetchSettings() call in the layout's onMount that already reads
// `polling.book_live_ms`/`polling.book_closed_ms`) has a ready-made,
// tested landing spot. Known incomplete: changing ONLY this module does
// NOT change the poller's actual cadence yet — `(algo)/+layout.svelte`
// still needs a companion edit to (a) read `polling.slow_ms` from
// `/api/admin/settings` and call `setFillWatchIntervalMs()`, and (b)
// change its `visibleInterval(pollOrderFillWatch, 5000)` call to read
// `getFillWatchIntervalMs()` instead of the hardcoded literal. Flagged
// explicitly in this change's report rather than silently left half-done.
let _fillWatchIntervalMs = 60_000;

/** Current fill-watch backstop cadence in ms. Default 60_000 (SLOW) —
 *  see module header comment above for the companion layout.svelte wiring
 *  this is designed to support. */
export function getFillWatchIntervalMs() {
  return _fillWatchIntervalMs;
}

/** Override the fill-watch backstop cadence (e.g. from a `polling.slow_ms`
 *  settings fetch). Guards the same >=1000ms floor as
 *  `setBookPollerLiveMs` — never accepts a sub-1s cadence. */
export function setFillWatchIntervalMs(ms) {
  if (!Number.isFinite(ms) || ms < 1000) return;
  _fillWatchIntervalMs = ms;
}
