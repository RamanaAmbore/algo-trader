/**
 * underlyingSpotStore — shared SSOT for underlying spot quotes fetched via
 * batchQuote. Both PositionStrip (NavStrip Exp P&L) and the derivatives page
 * write through this store so they share the same spot price for MCX futures
 * and index underlyings. Eliminates the NavStrip/derivatives Exp P&L divergence
 * caused by PositionStrip falling back to symbolStore (which only holds option
 * LTPs, not futures LTPs).
 *
 * Pattern: module-local $state, exported only via getter (same shape as
 * positionsDayPnlStore). Svelte 5 forbids exporting a reassigned $state
 * binding directly — wrap in an object with a get accessor instead.
 *
 * No polling logic here. Callers decide when to call loadUnderlyingSpots().
 */

import { batchQuote } from '$lib/api.js';
import { publishPulseQuotes } from '$lib/data/marketDataStores.svelte.js';
import { applyUnderlyingTickLtp, buildUnderlyingQuoteUpdate } from '$lib/data/underlyingQuoteUtils.js';
import { getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { resolveUnderlyingTradingsymbol, pickUnderlyingSpot } from '$lib/data/resolveUnderlying.js';
import { findNearestFuture } from '$lib/data/instruments.js';
import { cachedRead, cachedWrite, cachedDelete, TTL } from '$lib/data/persistentCache.js';

/**
 * Per-root epoch-ms of the last live-tick apply (patchUnderlyingSpot).
 * Used by buildUnderlyingQuoteUpdate to discard a batchQuote response's
 * `ltp` when a newer tick has already landed for that root while the
 * request was in flight — an ordering guard the plain object-spread merge
 * didn't have.
 * @type {Record<string, number>}
 */
let _lastTickAt = {};

/**
 * Module-local reactive map: { ROOT: { ltp, day_pct, prev_close } }
 * e.g. { CRUDEOIL: { ltp: 5788, day_pct: 0.4, prev_close: 5763 } }
 * @type {Record<string, { ltp: number, day_pct: number | null, prev_close: number }>}
 */
let _quotes = $state({});

// ── Tier-2 persistence (PLAN.md Part B, item B3) ───────────────────────
// This store previously had ZERO persistence — a reload wiped `_quotes`
// back to `{}`, so NavStrip's Exp P&L pill and the derivatives Payoff
// chart's spot/prev-close math both reset to 0 until the next
// loadUnderlyingSpots() round-trip landed. Keyed PER ROOT (not one blob
// for the whole map) because the set of tracked roots is page-decided
// (callers pass their own `pairs` to loadUnderlyingSpots) and unknown at
// module-eval time. Short TTL (`TTL.short`, 2 min) — this is live-ish
// spot data, not reference data; the cache exists only to bridge the gap
// until the next real fetch, not for off-hours persistence.
//
// Hydration deliberately does NOT live inside `getUnderlyingSpot()`.
// That function is called from inside `$derived.by(...)` blocks all over
// the app (e.g. portfolioStore.svelte.js's `_rootSpotCache`, always via
// `untrack(() => getUnderlyingSpot(root))` — `untrack` only suppresses
// DEPENDENCY TRACKING, it does NOT make a `$state` WRITE safe during a
// derived's evaluation). Mutating `_quotes` there would throw Svelte 5's
// `state_unsafe_mutation` the first time a reload hits a Tier-2 cache hit
// — exactly the CLAUDE.md "Reactive safety" hazard class. Hydration
// instead happens inside `loadUnderlyingSpots()` itself, which is always
// invoked from an `onMount`/`$effect` context (see PositionStrip.svelte,
// the sole caller) — mutating reactive `$state` from an effect is the
// normal, expected pattern. `getUnderlyingSpot()` stays a PURE read.
function _uspotCacheKey(/** @type {string} */ root) {
  return `uspot.${root}`;
}

/**
 * Returns the underlying spot LTP for the given root.
 * Falls back to 0 when root is not yet loaded.
 *
 * Resolves to the same front-month tradingsymbol Snapshot/Payoff use (via
 * resolveUnderlyingTradingsymbol) before checking symbolStore — this is
 * the fix for NavStrip's Exp P&L divergence from the derivatives page
 * (NavStrip was previously the only consumer reading the bare root
 * directly, missing the MCX-nearest-future / index-spot-key translation).
 * Falls through to the bare root (symbolStore, then the batchQuote cache)
 * when the resolver can't produce a live tick yet — e.g. a cold
 * instruments cache resolving an MCX/CDS root to its bare-root stub,
 * which never ticks under its own name.
 *
 * PURE read — see the Tier-2 persistence comment above for why hydration
 * must never happen in here.
 *
 * @param {string} root - e.g. "CRUDEOIL", "NIFTY", "GOLD"
 * @returns {number}
 */
export function getUnderlyingSpot(root) {
  return pickUnderlyingSpot(root, resolveUnderlyingTradingsymbol, getSnapshot, _quotes, findNearestFuture);
}

/**
 * Read-only handle to the reactive quote map. Use `.value` to access the
 * current snapshot — mirrors the dataStore pattern used elsewhere.
 */
export const underlyingSpotStore = {
  get value() { return _quotes; },
};

/**
 * Fetch batchQuote for the provided (root, quoteKey) pairs, populate the
 * shared store, and publish to symbolStore via publishPulseQuotes so live
 * option payoff charts and other symbolStore consumers stay in sync.
 *
 * @param {Array<{ root: string, quoteKey: string }>} pairs
 * @returns {Promise<void>}
 */
export async function loadUnderlyingSpots(pairs) {
  if (!pairs || pairs.length === 0) return;

  // Tier-2 hydrate (B3) — seed any root this call tracks that the live
  // map doesn't already have, from a cached quote, BEFORE the network
  // call. Safe to mutate `_quotes` here: `loadUnderlyingSpots` is always
  // invoked from an onMount/$effect context (PositionStrip.svelte), never
  // from inside a $derived — see the Tier-2 persistence comment near
  // `_uspotCacheKey` above for why `getUnderlyingSpot()` itself must stay
  // a pure read and can't do this hydration on its own. A root a caller
  // hasn't asked for in THIS call simply isn't seeded here; it's seeded
  // whenever some call (from any caller) first lists it in `pairs`.
  let _hydrated = _quotes;
  let _didHydrate = false;
  for (const p of pairs) {
    if (_hydrated[p.root] !== undefined) continue;
    try {
      const cached = cachedRead(_uspotCacheKey(p.root));
      const v = cached?.value;
      if (v && typeof v === 'object') {
        if (!_didHydrate) _hydrated = { ..._quotes };
        _hydrated[p.root] = v;
        _didHydrate = true;
      }
    } catch { /* localStorage unavailable (SSR / private mode) */ }
  }
  if (_didHydrate) _quotes = _hydrated;

  // Captured before the await so buildUnderlyingQuoteUpdate can detect a
  // live tick that landed for this root WHILE the request was in flight
  // (race guard — see underlyingQuoteUtils.js).
  const reqStartedAt = Date.now();
  const keys = pairs.map(p => p.quoteKey);
  const res = await batchQuote(keys);
  const items = res?.items ?? [];

  // Publish to symbolStore so liveSpot / OptionsPayoff consumers receive the
  // latest anchors without a separate batchQuote call.
  publishPulseQuotes(items);

  _quotes = buildUnderlyingQuoteUpdate(pairs, items, _quotes, _lastTickAt, reqStartedAt);

  // Tier-2 persist (B3) — one entry per root just fetched, not the whole
  // map, matching the per-root key scheme `getUnderlyingSpot` hydrates
  // from. A root this call didn't touch keeps whatever it already had.
  for (const p of pairs) {
    const v = _quotes[p.root];
    if (v !== undefined) {
      try { cachedWrite(_uspotCacheKey(p.root), v, TTL.short); } catch { /* ignore */ }
    }
  }
}

/**
 * Remove specific root entries from the store. Callers pass only the roots
 * THEY stopped tracking (e.g. a page's own previous subscription set minus
 * its current one) — never a blanket "keep only these" filter, since this
 * store is shared across pages (PositionStrip / derivatives) that each
 * track their own independent root set. A root removed here that another
 * page still needs is self-healing: that page's own periodic
 * loadUnderlyingSpots() re-populates it on its next poll cycle.
 *
 * @param {string[]} roots
 */
export function pruneUnderlyingSpotRoots(roots) {
  if (!roots || roots.length === 0) return;
  let changed = false;
  const next = { ..._quotes };
  for (const r of roots) {
    if (r in next) { delete next[r]; changed = true; }
    delete _lastTickAt[r];
    // Tier-2 cleanup (B3) — a pruned root's stale cached quote must not
    // get lazily re-hydrated by `getUnderlyingSpot` after some OTHER page
    // stops tracking it too and it ages past `TTL.short` anyway; deleting
    // it here is just a prompt version of the same eventual expiry.
    try { cachedDelete(_uspotCacheKey(r)); } catch { /* ignore */ }
  }
  if (changed) _quotes = next;
}

/**
 * Apply a single live-tick LTP patch to the store without triggering a full
 * batchQuote reload. Preserves day_pct and prev_close from the last batchQuote.
 * Used by tickBus handlers in the derivatives page so NavStrip (via getUnderlyingSpot)
 * also sees per-tick updates, not just 30s poll refreshes.
 *
 * No-op when root is not yet loaded (prevents phantom entries before first load).
 *
 * Records `lastTickAt[root]` whenever the incoming ltp is a valid live
 * observation (finite, > 0) — NOT gated on the store value actually
 * changing. An identical-value tick is still a fresher observation than
 * whatever in-flight batchQuote request may resolve after it, and
 * buildUnderlyingQuoteUpdate's ordering guard needs that timestamp to
 * detect a stale response even when the tick didn't move the price.
 *
 * @param {string} root - e.g. "CRUDEOIL", "NIFTY"
 * @param {number | null | undefined} ltp
 */
export function patchUnderlyingSpot(root, ltp) {
  const v = Number(ltp);
  if (Number.isFinite(v) && v > 0) _lastTickAt[root] = Date.now();
  const next = applyUnderlyingTickLtp(_quotes, root, ltp);
  if (next !== _quotes) {
    _quotes = next;
    // Tier-2 persist (B3) — a live tick is a real update too, not just a
    // batchQuote poll; without this, a reload immediately after a tick
    // (no poll in between) would still hydrate the pre-tick value.
    const updated = _quotes[root];
    if (updated !== undefined) {
      try { cachedWrite(_uspotCacheKey(root), updated, TTL.short); } catch { /* ignore */ }
    }
  }
}
