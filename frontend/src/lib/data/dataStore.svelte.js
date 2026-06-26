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
 *   store.lastFetch   — epoch-ms of last successful fetch ($state)
 *   store.load(opts)  — trigger fetch; opts.force=true skips dedup window
 *   store.invalidate()— wipe Tier 1 + localStorage; next load() re-fetches
 *   store.set(value)  — synchronous value set (for SSE / WebSocket pushes)
 */

import { cachedRead, cachedWrite, cachedDelete, TTL } from './persistentCache.js';

export { TTL };

/**
 * @template T
 * @param {{
 *   key: string,
 *   fetcher: (args?: any) => Promise<any>,
 *   ttl?: number,
 *   parse?: (raw: any) => T,
 *   equals?: (a: T | null, b: T | null) => boolean,
 * }} opts
 */
export function createDataStore({ key, fetcher, ttl = TTL.minute, parse = (r) => r, equals }) {
  // Default shallow-equality: primitives compare by ===; for arrays/objects
  // callers should pass a custom equals (e.g. deep-equal or length+first-item).
  const _eq = equals ?? ((a, b) => a === b);

  // ── Tier 1: in-memory reactive state ──────────────────────────────
  let _value   = $state(/** @type {T | null} */ (null));
  let _loading = $state(false);
  let _error   = $state(/** @type {string | null} */ (null));
  let _last    = $state(0); // epoch-ms of last successful fetch

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
  async function _fetch(args) {
    _loading = true;
    _error   = null;
    try {
      const raw  = await fetcher(args);
      const next = parse(raw);
      // Write to Tier 2 before updating state so a hypothetical render
      // during the microtask flush sees both consistent.
      if (next !== undefined && next !== null) {
        cachedWrite(key, next, ttl);
      }
      // Skip reactive write when value is reference-equal or passes
      // custom equality — prevents unnecessary downstream re-renders on
      // 30 s polls that return identical data.
      if (!_eq(_value, next)) {
        _value = next;
      }
      _last    = Date.now();
      _error   = null;
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
   * /admin/persistence/invalidate endpoint.
   */
  function invalidate() {
    _value   = null;
    _last    = 0;
    _error   = null;
    cachedDelete(key);
  }

  /**
   * Wipe Tier 1 only — keep Tier 2 (localStorage). The next load()
   * still goes straight to the fetcher (Tier 2 isn't consulted at
   * fetch time anyway), but a subsequent module-reinit will hydrate
   * from cache. Used by the SOFT refresh-cycle mode so the operator
   * sees stale-while-revalidate paint instead of a blank flash while
   * the broker re-fetches.
   */
  function softInvalidate() {
    _value = null;
    _last  = 0;
    _error = null;
  }

  /**
   * Synchronous value override for SSE / WebSocket pushes. Writes
   * through to Tier 2 so a page reload still shows the pushed value.
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

  return {
    get value()    { return _value;   },
    get loading()  { return _loading; },
    get error()    { return _error;   },
    get lastFetch(){ return _last;    },
    load,
    invalidate,
    softInvalidate,
    set,
  };
}
