/**
 * persistentCache — three-tier cache that survives BOTH within-session
 * navigation AND a full page reload (which is what happens on every
 * deploy when SvelteKit hands the browser a fresh JS bundle).
 *
 * Tiers, in read-order:
 *   1. In-memory Map   — instant; survives page navigation in the same
 *                        SvelteKit session (~0 cost)
 *   2. localStorage    — JSON-serialised; survives reload + deploy +
 *                        browser close (~1-3 ms read)
 *   3. Caller fetcher  — source of truth; runs in background and
 *                        updates both tiers on success
 *
 * Use case (operator: "when I switch pages, pulse doesn't show the
 * data immediately... is it possible to cache the data on frontend?
 * the data is retained during deployment"): MarketPulse, PositionStrip,
 * and any other dense data page paint immediately from cache while a
 * background fetcher refreshes. The grid stays interactive throughout.
 *
 * Industry analog: TradingView, IBKR TWS web, Sensibull, Streak all
 * use the same pattern — module-level in-memory + localStorage for
 * watchlists / sparklines / reference data; live position state still
 * always re-fetches on mount (canonical broker truth).
 *
 * NOT for live tick / SSE state — that lives in $lib/data/quoteStream
 * and reconnects from the WebSocket every mount.
 */

/** @type {Map<string, {value: any, refreshed_at: number, ttl_ms: number}>} */
const _mem = new Map();

/** Per-key pending localStorage write timers. Cleared + replaced on each
 *  cachedWrite call so rapid successive writes coalesce into one fsync. */
/** @type {Map<string, ReturnType<typeof setTimeout>>} */
const _lsTimers = new Map();

const PREFIX = 'rbq.cache.';

/**
 * Default TTL buckets (milliseconds). Picks match how stale the data
 * can safely be before the operator would be misled:
 *
 *   day      24 h — past-N-day closes, sparkline static portion. Static
 *                   until IST midnight rollover.
 *   hour      1 h — watchlist OHLC reference. Refreshes naturally.
 *   minute   15 m — positions / holdings / funds. Aggressively refreshed
 *                   on mount; the cache is only for instant paint.
 *   short     2 m — movers / quotes. Tighter window for live-ish data.
 */
export const TTL = {
  day:    24 * 60 * 60 * 1000,
  hour:        60 * 60 * 1000,
  minute: 15 *      60 * 1000,
  short:   2 *      60 * 1000,
};

function _now() { return Date.now(); }

/** Check if a cache entry has expired. Entries with ttl_ms=0 never expire. */
function _isExpired(entry) {
  if (!entry || typeof entry !== 'object') return true;
  if (!entry.ttl_ms) return false;
  return (_now() - (entry.refreshed_at || 0)) > entry.ttl_ms;
}

/** localStorage read with parse + expiry check. */
function _readLs(key) {
  if (typeof localStorage === 'undefined') return null;
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const entry = JSON.parse(raw);
    if (_isExpired(entry)) {
      try { localStorage.removeItem(PREFIX + key); } catch {}
      return null;
    }
    return entry;
  } catch {
    return null;
  }
}

/** localStorage write — silent on quota exceeded so the page never crashes. */
function _writeLs(key, entry) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(entry));
  } catch (e) {
    // QuotaExceededError most likely. Skip persistence; in-memory still works.
    // No retry, no logging — operator notification would be noisier than
    // the actual problem.
  }
}

/**
 * Read from cache (memory → localStorage). Returns null when not cached
 * or expired. Promotes localStorage hits into the in-memory map so
 * subsequent reads in the same session skip the parse cost.
 *
 * @param {string} key
 * @returns {{value: any, refreshed_at: number} | null}
 */
export function cachedRead(key) {
  const m = _mem.get(key);
  if (m && !_isExpired(m)) return m;
  if (m) _mem.delete(key);

  const ls = _readLs(key);
  if (ls) {
    _mem.set(key, ls);
    return ls;
  }
  return null;
}

/**
 * Write to both tiers. The in-memory Map is updated synchronously
 * (instant hot path). The localStorage write is debounced by 200ms
 * per key so rapid successive writes (e.g. MarketPulse calling
 * cachedWrite 5× per cycle) coalesce into a single synchronous
 * JSON.stringify + setItem — avoiding main-thread jank on mobile
 * Safari where localStorage writes block the event loop.
 *
 * Pass ttl_ms=0 for never-expire entries (rare — most data should
 * pick from TTL above).
 *
 * @param {string} key
 * @param {any} value
 * @param {number} ttl_ms
 */
export function cachedWrite(key, value, ttl_ms = TTL.minute) {
  const entry = { value, refreshed_at: _now(), ttl_ms };
  // Synchronous in-memory update — callers read from here on the hot path.
  _mem.set(key, entry);
  // Debounced localStorage write — cancel any pending write for this key
  // and schedule a fresh one. The closure captures `entry` by reference
  // so if another write lands within 200ms, we overwrite the timer with
  // the newer entry (clearTimeout + re-schedule).
  const existing = _lsTimers.get(key);
  if (existing !== undefined) clearTimeout(existing);
  _lsTimers.set(key, setTimeout(() => {
    _writeLs(key, entry);
    _lsTimers.delete(key);
  }, 200));
}

/**
 * Delete a single key from both tiers.
 *
 * @param {string} key
 */
export function cachedDelete(key) {
  _mem.delete(key);
  if (typeof localStorage === 'undefined') return;
  try { localStorage.removeItem(PREFIX + key); } catch {}
}

// staleStamp was spec-tested and dropped (zero callers in codebase).
// cachedDelete is kept for future use (e.g. cache invalidation on logout).
