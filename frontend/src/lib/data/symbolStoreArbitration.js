/**
 * symbolStoreArbitration — pure, rune-free helpers for symbolStore's
 * staleness-comparison arbitration.
 *
 * Extracted from symbolStore.svelte.js so it can be unit-tested directly:
 * .svelte.js files can't be imported by the current Vitest config (Svelte 5
 * rune compilation), which is why symbolStore has never had a real unit test.
 *
 * Problem this solves (primary defect — see docs/specs, PLAN.md):
 * symbolStore restores each symbol's last-tick timestamp (`ltp_ts`) from
 * localStorage on load. REST polls always write with `ltp_ts: 0`. The raw
 * staleness guard (`if (incomingTs < storedTs) continue`) means a restored
 * *yesterday's* `ltp_ts` permanently blocks every subsequent poll write for
 * that symbol — only a genuinely new SSE tick (which carries `Date.now()`)
 * can beat it. A symbol that hasn't ticked yet today (pre-market, evening
 * MCX-only window, illiquid contract, failed subscribe) then shows
 * yesterday's last price indefinitely, with no recovery short of a reload.
 *
 * Fix: gate the comparison on the trading-session boundary (00:00 IST
 * today), not raw timestamp ordering. A stored `ltp_ts` from *before*
 * today's session start is treated as 0 (unstamped) for comparison
 * purposes only — the stored value itself is never mutated, so it
 * remains valid for display/pruning until something newer arrives.
 */

import { startOfTodayIST } from '$lib/dateFormat.js';

/**
 * Day-memoized session boundary (epoch-ms of 00:00 IST "today"). Cheap to
 * call on every SSE tick / poll write — only recomputes (and re-invokes the
 * Intl formatter chain in `startOfTodayIST()`) when the wall clock has
 * crossed out of the cached [boundary, boundary + 24h) window, in either
 * direction (a clock stepped backward — e.g. in tests — must also
 * recompute, not just a forward rollover).
 *
 * @returns {number} epoch-ms of today's 00:00 IST boundary
 */
let _cachedBoundary = 0;
let _cachedBoundaryEnd = 0; // exclusive upper bound: _cachedBoundary + 24h
export function sessionBoundaryMs() {
  const now = Date.now();
  if (_cachedBoundary === 0 || now < _cachedBoundary || now >= _cachedBoundaryEnd) {
    _cachedBoundary = startOfTodayIST();
    _cachedBoundaryEnd = _cachedBoundary + 24 * 60 * 60 * 1000;
  }
  return _cachedBoundary;
}

/**
 * Returns the stored `ltp_ts` value to use for staleness *comparison*
 * purposes — 0 when the stored timestamp predates today's session boundary
 * (treated as "unstamped", so any poll/tick write is accepted), otherwise
 * the stored value unchanged.
 *
 * Never mutates or returns anything other than 0 or the input — this is a
 * comparison-time gate only, not a reset of the underlying stored value.
 *
 * @param {number} storedTs   - the symbol's currently-stored ltp_ts
 * @param {number} boundaryMs - session boundary from sessionBoundaryMs()
 * @returns {number}
 */
export function effectiveStoredLtpTs(storedTs, boundaryMs) {
  const ts = Number(storedTs);
  if (!Number.isFinite(ts) || ts <= 0) return 0;
  return ts < boundaryMs ? 0 : ts;
}
