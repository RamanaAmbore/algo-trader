/**
 * createDataStore — three-tier reactive store mirroring the backend
 * persistence pattern (memory → localStorage → broker).
 *
 * Tier 1: In-memory $state   — lives as long as the JS module is loaded.
 *                              Survives SvelteKit page navigation in the
 *                              same session (~0 read cost).
 * Tier 2: persistentCache    — localStorage-backed, TTL-bucketed JSON.
 *                              Survives full page reload + deploy. Populated
 *                              on module init so first paint is instant.
 * Tier 3: async fetcher      — source of truth. Background refresh after
 *                              serving from cache; writes back to both tiers.
 *
 * Concurrency: concurrent load() calls share one in-flight Promise so the
 * network round-trip is never doubled. A subsequent load() after the
 * previous resolves triggers a fresh fetch (next-tick scheduling ensures
 * the interval never stacks).
 *
 * Usage:
 *
 *   const store = createDataStore({
 *     key:     'md.positions',          // localStorage key + cache namespace
 *     fetcher: async () => fetchPositions(),
 *     ttl:     TTL.minute,
 *     parse:   r => r?.rows ?? [],      // optional payload selector
 *     equals:  (a, b) => a === b,       // optional change-detection
 *   });
 *
 *   store.value       — current value ($state, reactive)
 *   store.loading     — boolean ($state)
 *   store.error       — string | null ($state)
 *   store.lastFetch   — epoch-ms of last successful (non-degraded-empty) fetch
 *   store.meta        — { degraded, staleAccounts, asOf } ($state) — see
 *                        extractStaleMeta / the `meta` option below
 *   store.load(opts)  — trigger fetch; opts.force=true skips dedup window
 *   store.invalidate()— wipe Tier 1 + localStorage; next load() re-fetches
 *   store.softInvalidate() — mark stale (lastFetch=0) but KEEP the value
 *                        painted (stale-while-revalidate); next load() re-fetches
 *   store.set(value)  — synchronous value set (for SSE / WebSocket pushes);
 *                        bypasses parse()/meta() — see ingest() for raw responses
 *   store.ingest(raw) — feed an already-resolved raw backend response through
 *                        the same parse + degraded/empty guard as a fetcher-
 *                        driven load() (for callers that fetch the raw
 *                        response themselves, e.g. PerformancePage.loadAll)
 */

import { cachedRead, cachedWrite, cachedDelete, TTL } from './persistentCache.js';

export { TTL };

/**
 * Stale-while-valid empty detector. Treats Array(0) and {} as empty.
 * Other shapes (primitives, non-empty arrays/objects, null/undefined
 * are handled separately) are not "empty" in this sense.
 * Hoisted to module scope (not a closure inside createDataStore) so it
 * can be unit-tested directly without invoking any $state-bearing code
 * (vitest has no svelte-compiler plugin registered — see
 * frontend/src/lib/__tests__/data/dataStore.test.js).
 * @param {any} v
 */
export function isEmptyValue(v) {
  if (v == null) return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === 'object') return Object.keys(v).length === 0;
  return false;
}

/**
 * Extract the degraded-fetch signal from a raw backend response.
 *
 * Real-money guard (2026-09, NavStrip "0 instead of last-known-good"
 * fix): `PositionsResponse` / `HoldingsResponse` / `FundsResponse` all
 * carry `stale_accounts: string[]` — accounts substituted from the
 * broker_apis last-known-good frame cache because their circuit
 * breaker was open at fetch time (see backend/api/schemas.py). A
 * non-empty list means SOME or ALL of this response's rows are
 * substituted/stale, not a genuine fresh read — the frontend must not
 * treat it as confirmed-fresh data (see createDataStore's `meta`
 * option below).
 *
 * `metaFn`, when supplied, is a per-store extractor `(raw) => ({
 * staleAccounts, asOf })` — callers pass one so this stays generic
 * across positions/holdings/funds response shapes rather than hard-
 * coding `stale_accounts`/`as_of` field names here.
 *
 * @param {any} raw
 * @param {((raw: any) => { staleAccounts?: string[], asOf?: string|null }) | undefined} metaFn
 * @returns {{ degraded: boolean, staleAccounts: string[], asOf: string|null }}
 */
export function extractStaleMeta(raw, metaFn) {
  const m = typeof metaFn === 'function' ? (metaFn(raw) || {}) : null;
  const staleAccounts = Array.isArray(m?.staleAccounts) ? m.staleAccounts : [];
  const asOf = m?.asOf ?? null;
  return { degraded: staleAccounts.length > 0, staleAccounts, asOf };
}

/**
 * @template T
 * @param {{
 *   key: string,
 *   fetcher: (args?: any) => Promise<any>,
 *   ttl?: number,
 *   parse?: (raw: any) => T,
 *   equals?: (a: T | null, b: T | null) => boolean,
 *   keepStaleOnEmpty?: boolean,
 *   meta?: (raw: any) => { staleAccounts?: string[], asOf?: string|null },
 * }} opts
 *
 * keepStaleOnEmpty (default false) — stale-while-valid guard. When the
 * fetcher returns an empty payload (Array length=0, plain-object key
 * count=0) AND _value already holds a non-empty value, the write is
 * suppressed so a transient broker hiccup / off-hours empty response
 * doesn't blank out a populated grid. Hydration-race fix for /pulse:
 * movers panel was emptying intermittently when the batchQuote call
 * landed during a moment where every symbol had pct=0 (just-opened
 * market, broker rate-limit). Operator-visible as "winners/gainers
 * rows become empty and show data on and off". Keep set() unconditional
 * — explicit writes (SSE pushes, PerformancePage post-fetch) bypass
 * this guard intentionally.
 *
 * meta (optional) — extractor for the degraded-fetch signal (see
 * extractStaleMeta above). When supplied, an empty-and-degraded
 * response is treated the same as keepStaleOnEmpty (prior non-empty
 * value is retained) REGARDLESS of the keepStaleOnEmpty flag — this is
 * intentionally narrower than a blanket keepStaleOnEmpty: a response
 * that is empty AND carries no stale_accounts tag is a genuine empty
 * book (operator closed everything, or the 08:00 daily rollover) and
 * IS allowed to overwrite with a real 0. A degraded response is never
 * written to Tier 2 (localStorage) — see _applyRaw below — so a
 * masked broker failure can't poison the disk cache for the next page
 * load. Exposed via `store.meta` so consumers (PositionStrip,
 * portfolioStore) can distinguish "confirmed empty" from "degraded"
 * without re-deriving the predicate themselves.
 */
export function createDataStore({ key, fetcher, ttl = TTL.minute, parse = (r) => r, equals, keepStaleOnEmpty = false, meta }) {
  // Default shallow-equality: primitives compare by ===; for arrays/objects
  // callers should pass a custom equals (e.g. deep-equal or length+first-item).
  const _eq = equals ?? ((a, b) => a === b);

  // ── Tier 1: in-memory reactive state ──────────────────────────────
  let _value   = $state(/** @type {T | null} */ (null));
  let _loading = $state(false);
  let _error   = $state(/** @type {string | null} */ (null));
  let _last    = $state(0); // epoch-ms of last successful (non-degraded-empty) fetch
  // Degraded-fetch signal (real-money guard) — see extractStaleMeta doc.
  // Defaults to non-degraded so stores that don't pass `meta` behave
  // exactly as before (keepStaleOnEmpty is the only guard in play).
  let _meta = $state(
    /** @type {{ degraded: boolean, staleAccounts: string[], asOf: string|null }} */
    ({ degraded: false, staleAccounts: [], asOf: null })
  );

  // ── In-flight dedup ───────────────────────────────────────────────
  /** @type {Promise<void> | null} */
  let _inflight = null;
  // JSON-serialised args for the current in-flight request. Two
  // concurrent load([1,2,3]) calls share the same Promise; a
  // concurrent load([4,5]) with different args starts a fresh fetch.
  let _inflightArgsKey = /** @type {string | undefined} */ (undefined);

  // ── Initialise from Tier 2 synchronously ──────────────────────────
  // Run once at module-evaluation time so every component that reads
  // store.value immediately (before their onMount) sees cached data.
  (function _initFromCache() {
    try {
      const cached = cachedRead(key);
      if (cached?.value !== undefined && cached.value !== null) {
        _value = cached.value;
        _last  = cached.refreshed_at ?? 0;
      }
    } catch { /* localStorage unavailable (SSR / private mode) */ }
  })();

  // ── Core fetch ───────────────────────────────────────────────────
  // Stale-while-valid empty detector — hoisted to module scope as
  // isEmptyValue() above; kept as a local alias for the existing call
  // sites below (no behaviour change).
  const _isEmpty = isEmptyValue;

  /**
   * Apply an already-resolved raw response through parse + the
   * degraded/empty guards. Shared by both _fetch() (fetcher-driven)
   * and ingest() (caller already has the raw response — e.g.
   * PerformancePage.loadAll, which fetches positions/holdings/funds
   * itself via Promise.allSettled and must feed the SAME guarded path
   * these module-level singletons use everywhere else — see A3 fix #4,
   * "PerformancePage .set() bypasses every store guard").
   * @param {any} raw
   */
  function _applyRaw(raw) {
    const next = parse(raw);
    const staleMeta = extractStaleMeta(raw, meta);
    _meta = staleMeta;
    // Stale-while-valid guard (hydration races, Jun 2026) OR a backend-
    // tagged degraded response (real-money guard, 2026-09): when the
    // fresh value is empty AND the prior value was populated, KEEP the
    // prior. `keepStaleOnEmpty` covers the generic transient-empty
    // case; `staleMeta.degraded` covers a specific backend signal that
    // this exact response is a masked/substituted failure, not a
    // genuine empty book — see extractStaleMeta's doc comment for why
    // this is intentionally narrower than a blanket keepStaleOnEmpty.
    const dropEmpty = (keepStaleOnEmpty || staleMeta.degraded)
      && _isEmpty(next)
      && _value != null
      && !_isEmpty(_value);
    if (dropEmpty) {
      _error = null;
      // Only bump _last for the plain keepStaleOnEmpty path. A
      // degraded response did NOT actually land fresh data — bumping
      // _last here would make `lastFetch` (used for STALE@HH:MM
      // badges) claim the data is current when it's really frozen at
      // whatever _last already held.
      if (!staleMeta.degraded) _last = Date.now();
      return;
    }
    // Never persist a degraded response to Tier 2 — writing a masked/
    // substituted payload to localStorage would poison the disk cache
    // for the next page load/reload, long after this in-memory guard
    // has moved on. A non-degraded response still writes through as
    // before.
    if (next !== undefined && next !== null && !staleMeta.degraded) {
      cachedWrite(key, next, ttl);
    }
    // Skip reactive write when value is reference-equal or passes
    // custom equality — prevents unnecessary downstream re-renders on
    // 30 s polls that return identical data.
    if (!_eq(_value, next)) {
      _value = next;
    }
    _last  = Date.now();
    _error = null;
  }

  async function _fetch(args) {
    _loading = true;
    _error   = null;
    try {
      const raw = await fetcher(args);
      _applyRaw(raw);
    } catch (e) {
      _error = (e && typeof e === 'object' && 'message' in e)
        ? String(/** @type {any} */ (e).message).slice(0, 120)
        : 'Fetch failed';
      // Leave _value at last-good — stale-while-error semantics.
    } finally {
      _loading         = false;
      _inflight        = null;
      _inflightArgsKey = undefined;
    }
  }

  // ── Public API ───────────────────────────────────────────────────

  /**
   * Trigger a background fetch.
   *
   * load(args?, opts?) — two optional parameters:
   *
   *   args — passed verbatim to the fetcher. Concurrent calls with
   *     identical args (compared via JSON.stringify) share the in-flight
   *     Promise. Calls with different args start a fresh fetch immediately.
   *     Omit args for fetchers that take no parameters.
   *
   *   opts.force = true — always start a fresh fetch even if an in-flight
   *     request for the same args is already running (useful for manual
   *     refresh buttons where the operator explicitly wants new data).
   *
   * @param {any} [args]
   * @param {{ force?: boolean }} [opts]
   * @returns {Promise<void>}
   */
  function load(args, opts = {}) {
    // Support the legacy zero-arg call signature load() and the
    // opts-only call load({force: true}) used by existing callers.
    // Distinguish by checking whether `args` is a plain options object
    // (has a `force` key and no other "data-like" structure).
    let _args = args;
    let _opts = opts;
    if (args !== undefined && !Array.isArray(args) && typeof args === 'object' && 'force' in args && Object.keys(args).every(k => k === 'force')) {
      _opts = /** @type {{ force?: boolean }} */ (args);
      _args = undefined;
    }
    const argsKey = _args !== undefined ? JSON.stringify(_args) : undefined;
    if (_inflight && !_opts.force && argsKey === _inflightArgsKey) return _inflight;
    _inflightArgsKey = argsKey;
    _inflight = _fetch(_args);
    return _inflight;
  }

  /**
   * Wipe Tier 1 + Tier 2. The next load() call will skip both cache
   * tiers and go straight to the fetcher. This is the "hard" path —
   * used by the HARD refresh-cycle mode and the per-store
   * /admin/persistence/invalidate endpoint. A genuine full reset
   * (operator-triggered, also recycles the broker ticker) — unlike
   * softInvalidate below, this intentionally clears _value: the
   * operator explicitly asked for a clean slate.
   */
  function invalidate() {
    _value   = null;
    _last    = 0;
    _error   = null;
    _meta    = { degraded: false, staleAccounts: [], asOf: null };
    cachedDelete(key);
  }

  /**
   * Wipe Tier 1's freshness bookkeeping only — keep the in-memory
   * value AND Tier 2 (localStorage). The next load() still goes
   * straight to the fetcher, but the operator keeps seeing the last-
   * good value (stale-while-revalidate paint) instead of a blank
   * flash while the broker re-fetches. Used by the SOFT refresh-cycle
   * mode.
   *
   * Real-money fix (2026-09): this used to null `_value`, contradicting
   * this very doc comment and defeating both `keepStaleOnEmpty` and the
   * degraded guard above (both check `_value != null`) for every store
   * on the very next fetch after a soft reset — `portfolioAggregates`'
   * 0-on-null getters would then flash to 0 for every P&L/cash/margin
   * figure until the next poll landed. `_last = 0` still marks the
   * value as due-for-refresh (freshness checks elsewhere use lastFetch,
   * not a null-check on value) without discarding what's on screen.
   */
  function softInvalidate() {
    _last  = 0;
    _error = null;
  }

  /**
   * Synchronous value override for SSE / WebSocket pushes. Writes
   * through to Tier 2 so a page reload still shows the pushed value.
   * Bypasses parse()/meta() — callers that already have the raw
   * backend response (carrying stale_accounts/as_of) should use
   * ingest() instead so the degraded guard still applies.
   *
   * @param {T} value
   */
  function set(value) {
    if (!_eq(_value, value)) {
      _value = value;
    }
    cachedWrite(key, value, ttl);
    _last = Date.now();
  }

  /**
   * Feed an already-resolved raw backend response through the SAME
   * parse + degraded/empty-guard pipeline a fetcher-driven load() uses
   * (see _applyRaw doc above). Use this instead of set() whenever the
   * caller fetched the raw response itself (bypassing this store's own
   * `fetcher`) — e.g. PerformancePage.loadAll, which Promise.allSettled
   * s its own /positions, /holdings, /funds calls and must not clobber
   * these module-level singletons with an unguarded write (A3 fix #4).
   *
   * @param {any} raw
   */
  function ingest(raw) {
    _applyRaw(raw);
  }

  return {
    get value()    { return _value;   },
    get loading()  { return _loading; },
    get error()    { return _error;   },
    get lastFetch(){ return _last;    },
    /** { degraded, staleAccounts, asOf } — see extractStaleMeta doc. */
    get meta()     { return _meta;    },
    load,
    invalidate,
    softInvalidate,
    set,
    ingest,
  };
}
