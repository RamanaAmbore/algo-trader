/**
 * fillWatchIntervalMs.test.js — coverage for marketDataStores.svelte.js's
 * `getFillWatchIntervalMs` / `setFillWatchIntervalMs` pair (Fix 5, 2026-10).
 *
 * marketDataStores.svelte.js has top-level `$state(...)` calls that
 * execute at module-eval time, so it can't be imported directly in this
 * harness (no svelte-compiler plugin in vitest.config.js — same
 * constraint documented in marketDataStoresMeta.test.js). Two-part
 * coverage instead:
 *  1. A pure-function mirror of the getter/setter pair's trivial guard
 *     logic (>=1000ms floor, default 60_000).
 *  2. A source-scan (Vite `?raw` import — same convention as
 *     marketDataStoresMeta.test.js) confirming the real shipped file:
 *       - defaults `_fillWatchIntervalMs` to 60_000 (the SLOW cadence
 *         this fix changes the fill-watch backstop poller to, from its
 *         previous 5_000 FAST default)
 *       - exports both `getFillWatchIntervalMs` and `setFillWatchIntervalMs`
 *       - the setter guards the same >=1000ms floor as the sibling
 *         `setBookPollerLiveMs`/`setBookPollerClosedMs` pair
 *
 * Known scope gap (documented in the shipped code comment too): this
 * module only provides the getter/setter landing spot for a FUTURE
 * `polling.slow_ms` settings-driven wiring in `(algo)/+layout.svelte`
 * (out of file-scope for this change) — it does not itself change
 * `pollOrderFillWatch`'s live cadence, since that interval is owned by
 * `visibleInterval(pollOrderFillWatch, 5000)` in the layout, not here.
 *
 * Five quality dimensions:
 *  1. SSOT   — source-scan reads the real shipped file, not a stale copy.
 *  2. Perf   — pure function + string reads, no I/O.
 *  3. Stale  — regression-guards the EXACT default value (60_000) this
 *              fix changes, not just presence of the functions.
 *  4. Reuse  — mirrors the identical floor-guard convention already
 *              proven by setBookPollerLiveMs/setBookPollerClosedMs.
 *  5. UX     — the fill-watch poller is a pure backstop (position_filled
 *              WS + OrderBook/LogPanel's own polls are primary per
 *              CLAUDE.md); 60s cadence avoids 12x-wasted-request/min
 *              overhead the previous 5s default carried for a backstop.
 */

import { describe, it, expect } from 'vitest';
import src from '$lib/data/marketDataStores.svelte.js?raw';

/** Pure mirror of the shipped getter/setter pair's guard logic. */
function makeFillWatchIntervalState(initial = 60_000) {
  let ms = initial;
  return {
    get: () => ms,
    set: (next) => {
      if (!Number.isFinite(next) || next < 1000) return;
      ms = next;
    },
  };
}

describe('getFillWatchIntervalMs / setFillWatchIntervalMs — pure logic mirror', () => {
  it('defaults to 60_000 (SLOW backstop cadence)', () => {
    const state = makeFillWatchIntervalState();
    expect(state.get()).toBe(60_000);
  });

  it('accepts a valid override (e.g. a polling.slow_ms settings value)', () => {
    const state = makeFillWatchIntervalState();
    state.set(45_000);
    expect(state.get()).toBe(45_000);
  });

  it('rejects a sub-1000ms value, keeping the previous value', () => {
    const state = makeFillWatchIntervalState();
    state.set(500);
    expect(state.get()).toBe(60_000);
  });

  it('rejects non-finite input (NaN / Infinity / undefined)', () => {
    const state = makeFillWatchIntervalState();
    state.set(NaN);
    expect(state.get()).toBe(60_000);
    state.set(Infinity);
    expect(state.get()).toBe(60_000);
    state.set(undefined);
    expect(state.get()).toBe(60_000);
  });
});

describe('marketDataStores.svelte.js source — fill-watch cadence wiring', () => {
  it('defaults _fillWatchIntervalMs to 60_000, not the old 5_000 FAST literal', () => {
    expect(/let _fillWatchIntervalMs\s*=\s*60_000;/.test(src)).toBe(true);
  });

  it('exports getFillWatchIntervalMs returning the module-level value', () => {
    expect(/export function getFillWatchIntervalMs\(\)\s*\{\s*return _fillWatchIntervalMs;\s*\}/.test(src))
      .toBe(true);
  });

  it('exports setFillWatchIntervalMs guarded by the same >=1000ms floor as setBookPollerLiveMs', () => {
    const setterMatch = src.match(
      /export function setFillWatchIntervalMs\(ms\)\s*\{\s*if \(!Number\.isFinite\(ms\) \|\| ms < 1000\) return;\s*_fillWatchIntervalMs = ms;\s*\}/
    );
    expect(setterMatch).toBeTruthy();
  });
});
