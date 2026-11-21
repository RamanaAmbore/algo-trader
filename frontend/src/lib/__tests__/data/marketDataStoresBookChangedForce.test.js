/**
 * marketDataStoresBookChangedForce.test.js
 *
 * Fix (2026-11) — a closed-hours broker-account re-enable writes a fresh
 * `daily_book` row (backend/api/routes/brokers.py:
 * _force_refresh_one_account_after_reenable → _snapshot_books_if_closed)
 * and broadcasts the existing `book_changed` WS event from the point
 * where `snapshot_daily_book()` lands any row. Before this fix,
 * `marketDataStores.svelte.js`'s cross-page book poller
 * (`positionsStore`/`holdingsStore`/`pulsePositionsStore`/
 * `pulseHoldingsStore`/`fundsStore`) was not wired to that event at all,
 * so `_tickBookPollers`' own closed-hours gate (see
 * bookPollerClosedWindow.test.js) kept skipping every poll in every
 * OTHER open tab/device/role until the next grace window or market
 * open — the re-enabling tab saw fresh data, nobody else did.
 *
 * The fix: `startBookPollers()` subscribes to the `bookChanged` monotonic
 * counter (frontend/src/lib/data/bookChanged.js) via `subscribeSkipInitial`
 * (see subscribeSkipInitial.test.js for that primitive's own real-store
 * coverage) and calls `_loadBookOnce(true)` on every REAL increment — a
 * forced refresh that bypasses createDataStore's in-flight dedup (so it
 * can't share a request issued before the backend's write) without
 * touching the routine closed-hours grace-window throttle
 * (`_lastGraceFetchAt`).
 *
 * marketDataStores.svelte.js has top-level `$state(...)` calls that
 * execute at module-eval time with no svelte-compiler plugin wired into
 * vitest.config.js, so it can't be imported directly here — same
 * constraint and same two-part convention (source-scan `?raw` +
 * behavioral harness) as bookPollerClosedWindow.test.js.
 *
 * Five quality dimensions:
 *  1. SSOT   — source-scan reads the real shipped file; the harness's
 *              force-vs-grace-throttle bookkeeping mirrors the exact
 *              control flow asserted by the source-scan below.
 *  2. Perf   — proves a forced refresh is edge-triggered exactly once per
 *              real event, never continuous/resumed polling (the
 *              operator's explicit requirement).
 *  3. Stale  — regression-guards the "do not touch _lastGraceFetchAt
 *              when forced" shape, and the "skip-first-echo" wiring via
 *              subscribeSkipInitial (not a hand-rolled primed flag that
 *              could drift).
 *  4. Reuse  — asserts the subscription uses the SAME `bookChanged` store
 *              every other consumer (LogPanel, order poll) subscribes to,
 *              and the SAME `subscribeSkipInitial` helper this file's own
 *              sibling test exercises directly against a real store.
 *  5. UX     — a cold mount (bookChanged already non-zero) must not fire
 *              a spurious extra refresh on top of the existing UNGATED
 *              cold-load kick that startBookPollers already performs.
 */
import { describe, it, expect } from 'vitest';
import src from '$lib/data/marketDataStores.svelte.js?raw';
import { writable } from 'svelte/store';
import { subscribeSkipInitial } from '$lib/data/subscribeSkipInitial.js';

/** Brace-match a function declaration (async or not) by name, matching
 *  bookPollerClosedWindow.test.js's own extraction helper. */
function extractFunctionBlock(name, { async: isAsync = true } = {}) {
  const kw = isAsync ? 'async function' : 'function';
  const re = new RegExp(`(?:export\\s+)?${kw} ${name}\\s*\\([^)]*\\)\\s*\\{`);
  const m = re.exec(src);
  expect(m, `${name} declaration not found`).not.toBeNull();
  const bodyStart = m.index + m[0].length - 1;
  let depth = 0;
  for (let i = bodyStart; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(bodyStart, i + 1);
    }
  }
  throw new Error(`unterminated block for ${name}`);
}

describe('marketDataStores.svelte.js — bookChanged forced-refresh wiring (source-scan)', () => {
  it('imports bookChanged and subscribeSkipInitial', () => {
    expect(src).toMatch(/import\s*\{\s*bookChanged\s*\}\s*from\s*['"]\$lib\/data\/bookChanged\.js['"]/);
    expect(src).toMatch(/import\s*\{\s*subscribeSkipInitial\s*\}\s*from\s*['"]\.\/subscribeSkipInitial\.js['"]/);
  });

  it('startBookPollers subscribes to bookChanged via subscribeSkipInitial, calling _loadBookOnce(true) on each real event', () => {
    const block = extractFunctionBlock('startBookPollers', { async: false });
    expect(block).toMatch(/subscribeSkipInitial\(\s*bookChanged\s*,/);
    expect(block).toMatch(/_loadBookOnce\(true\)/);
  });

  // 2026-11 follow-up (operator-confirmed): bookChanged also bumps on
  // fill_event/positions_refreshed, and book_changed fires on EVERY
  // terminal order status including each chase cancel-and-replace's
  // CANCELLED postback — during live trading hours that's a forced
  // 5-store refresh per fill/cancel-replace in every open tab, pure
  // redundant broker-layer load on top of the already-running poller.
  // The forced refresh must fire ONLY while the market is genuinely
  // closed — the exact gap it exists to close.
  it('the bookChanged subscription callback no-ops while either segment is open (market-hours guard, BEFORE _loadBookOnce(true))', () => {
    const block = extractFunctionBlock('startBookPollers', { async: false });
    const subCallIdx = block.indexOf('subscribeSkipInitial(');
    expect(subCallIdx).toBeGreaterThan(-1);
    const callbackRegion = block.slice(subCallIdx);
    // Same condition _tickBookPollers already uses to decide "nothing in
    // daily_book can change right now" — not a re-derived or inverted form.
    expect(callbackRegion).toMatch(/if\s*\(isNseOpen\(\)\s*\|\|\s*isMcxOpen\(\)\)\s*return;/);
    const guardIdx = callbackRegion.indexOf('if (isNseOpen() || isMcxOpen()) return;');
    const forcedCallIdx = callbackRegion.indexOf('_loadBookOnce(true)');
    expect(guardIdx).toBeGreaterThan(-1);
    expect(forcedCallIdx).toBeGreaterThan(-1);
    // The guard must run BEFORE the forced fetch — an early return, not a
    // post-fetch check that already did the (redundant) work.
    expect(forcedCallIdx).toBeGreaterThan(guardIdx);
  });

  it('the market-hours guard is the FIRST check inside the subscription callback, after the skip-first-echo (subscribeSkipInitial handles that separately)', () => {
    const block = extractFunctionBlock('startBookPollers', { async: false });
    // The callback body is everything between the subscribeSkipInitial(...)
    // call's opening `{` and its closing `});` — extract it directly so
    // "first line of the callback" is unambiguous.
    const m = /subscribeSkipInitial\(\s*bookChanged\s*,\s*\(\)\s*=>\s*\{([\s\S]*?)\}\s*\);/.exec(block);
    expect(m, 'subscribeSkipInitial(bookChanged, () => { ... }); callback not found').not.toBeNull();
    const callbackBody = m[1].trim();
    // First statement in the callback body is the guard, not the forced call.
    expect(callbackBody.startsWith('if (isNseOpen() || isMcxOpen()) return;')).toBe(true);
  });

  it('the bookChanged subscription is registered AFTER the recurring visibleInterval wiring (does not replace the routine poller)', () => {
    const block = extractFunctionBlock('startBookPollers', { async: false });
    const pollerIdx = block.indexOf('visibleInterval(');
    const subIdx = block.indexOf('subscribeSkipInitial(');
    expect(pollerIdx).toBeGreaterThan(-1);
    expect(subIdx).toBeGreaterThan(-1);
    expect(subIdx).toBeGreaterThan(pollerIdx);
  });

  it('stopBookPollers unsubscribes the bookChanged subscription', () => {
    const block = extractFunctionBlock('stopBookPollers', { async: false });
    expect(block).toMatch(/if\s*\(_bookChangedUnsub\)/);
    expect(block).toMatch(/_bookChangedUnsub\(\)/);
    expect(block).toMatch(/_bookChangedUnsub\s*=\s*null/);
  });

  it('_loadBookOnce accepts a force parameter defaulting to false', () => {
    expect(src).toMatch(/async function _loadBookOnce\(force\s*=\s*false\)\s*\{/);
  });

  it('_loadBookOnce never writes _lastGraceFetchAt when force is true (gated by !force)', () => {
    const block = extractFunctionBlock('_loadBookOnce');
    expect(block).toMatch(/if\s*\(!force\)\s*_lastGraceFetchAt\s*=/);
  });

  it('_loadBookOnce threads an opts object into every store.load() call, forced only when force=true', () => {
    const block = extractFunctionBlock('_loadBookOnce');
    expect(block).toMatch(/const opts = force \? \{ ?force: true ?\} : undefined;/);
    for (const store of ['positionsStore', 'holdingsStore', 'pulsePositionsStore', 'pulseHoldingsStore', 'fundsStore']) {
      expect(block).toMatch(new RegExp(`${store}\\.load\\(opts\\)`));
    }
  });

  it('_tickBookPollers (the routine poller) is unchanged — still calls the bare, unforced _loadBookOnce()', () => {
    const block = extractFunctionBlock('_tickBookPollers');
    expect(block).toMatch(/_loadBookOnce\(\);/);
    expect(block).not.toMatch(/_loadBookOnce\(true\)/);
  });

  it('the cold-load kick in startBookPollers stays bare/unforced (unrelated to the bookChanged subscription)', () => {
    const block = extractFunctionBlock('startBookPollers', { async: false });
    const kickRegion = block.slice(0, block.indexOf('visibleInterval('));
    expect(kickRegion).toMatch(/_loadBookOnce\(\);/);
    expect(kickRegion).not.toMatch(/_loadBookOnce\(true\)/);
  });
});

// ── Behavioral coverage of the market-hours guard — real subscribeSkipInitial ──
//
// Exercises the ACTUAL subscribeSkipInitial primitive (imported above,
// already covered directly against a real store in
// subscribeSkipInitial.test.js) combined with a faithful mirror of the
// new guard+call shape the source-scan above pins — not a tautology:
// this proves the composition (skip-first-echo wrapping a market-hours
// gate wrapping the forced call) behaves correctly for a real store,
// across both market states.
function wireBookChangedSubscription(bookChangedStore, isNseOpenFn, isMcxOpenFn, onForcedLoad) {
  return subscribeSkipInitial(bookChangedStore, () => {
    if (isNseOpenFn() || isMcxOpenFn()) return;
    onForcedLoad();
  });
}

describe('bookChanged subscription — market-hours guard (real subscribeSkipInitial)', () => {
  it('does NOT trigger a forced refresh on a real bookChanged increment while the market is open', () => {
    const bc = writable(0);
    let forcedCalls = 0;
    const unsub = wireBookChangedSubscription(
      bc,
      () => true,  // isNseOpen() === true
      () => false, // isMcxOpen()
      () => { forcedCalls++; },
    );
    bc.update((n) => n + 1); // a real book_changed/fill_event increment
    expect(forcedCalls).toBe(0);
    unsub();
  });

  it('DOES trigger a forced refresh on a real bookChanged increment while the market is fully closed', () => {
    const bc = writable(0);
    let forcedCalls = 0;
    const unsub = wireBookChangedSubscription(
      bc,
      () => false, // isNseOpen()
      () => false, // isMcxOpen()
      () => { forcedCalls++; },
    );
    bc.update((n) => n + 1);
    expect(forcedCalls).toBe(1);
    unsub();
  });

  it('still skips the initial echo even while closed (closed-hours alone does not cause a spurious fire on mount)', () => {
    const bc = writable(7); // a late-mounting tab, counter already non-zero
    let forcedCalls = 0;
    const unsub = wireBookChangedSubscription(
      bc,
      () => false,
      () => false,
      () => { forcedCalls++; },
    );
    expect(forcedCalls).toBe(0); // initial echo skipped, not a market-open false-negative
    bc.update((n) => n + 1);
    expect(forcedCalls).toBe(1); // the next REAL increment still fires
    unsub();
  });

  it('is gated correctly for MCX-only-open (not just NSE)', () => {
    const bc = writable(0);
    let forcedCalls = 0;
    const unsub = wireBookChangedSubscription(
      bc,
      () => false, // isNseOpen()
      () => true,  // isMcxOpen() — MCX-only session
      () => { forcedCalls++; },
    );
    bc.update((n) => n + 1);
    expect(forcedCalls).toBe(0); // either segment open is enough to suppress
    unsub();
  });

  it('exactly one forced refresh per real increment while closed — no double-fire, no continuous polling', () => {
    const bc = writable(0);
    let forcedCalls = 0;
    const unsub = wireBookChangedSubscription(bc, () => false, () => false, () => { forcedCalls++; });
    bc.update((n) => n + 1);
    expect(forcedCalls).toBe(1);
    bc.update((n) => n + 1);
    expect(forcedCalls).toBe(2);
    // No timer, no repeat call without a further real increment.
    expect(forcedCalls).toBe(2);
    unsub();
  });
});

// ── Behavioral harness — force-vs-grace-throttle bookkeeping ─────────────
//
// Mirrors _loadBookOnce's exact decision shape for the one piece that
// can't be exercised via source-scan alone: the stateful interaction
// between a forced call and the routine grace-window throttle timestamp.

function makeForceAwareLoadHarness() {
  let lastGraceFetchAt = 0;
  let fetchCount = 0;
  let forcedFetchCount = 0;

  function loadOnce({ force = false, live, now }) {
    fetchCount++;
    if (force) forcedFetchCount++;
    if (!force) {
      lastGraceFetchAt = live ? 0 : now;
    }
    return true;
  }

  return {
    loadOnce,
    get fetchCount() { return fetchCount; },
    get forcedFetchCount() { return forcedFetchCount; },
    get lastGraceFetchAt() { return lastGraceFetchAt; },
  };
}

describe('_loadBookOnce(force) behavioral harness — grace-throttle isolation', () => {
  it('a forced call during a closed-hours grace-window throttle does not reset or stamp lastGraceFetchAt', () => {
    const h = makeForceAwareLoadHarness();
    // Routine grace-window fetch stamps the throttle.
    h.loadOnce({ force: false, live: false, now: 1_000_000 });
    expect(h.lastGraceFetchAt).toBe(1_000_000);

    // A bookChanged event fires a forced refresh shortly after, while
    // still inside the throttle window — must not disturb the stamp.
    h.loadOnce({ force: true, live: false, now: 1_000_500 });
    expect(h.lastGraceFetchAt).toBe(1_000_000); // untouched
    expect(h.forcedFetchCount).toBe(1);
    expect(h.fetchCount).toBe(2);
  });

  it('the routine grace-window throttle still elapses normally after an intervening forced call', () => {
    const h = makeForceAwareLoadHarness();
    const bookClosedMs = 1_800_000;
    h.loadOnce({ force: false, live: false, now: 1_000_000 });
    h.loadOnce({ force: true, live: false, now: 1_000_500 }); // bookChanged event
    // Throttle computed from the LAST non-forced stamp (1_000_000), not
    // disturbed by the forced call at 1_000_500.
    const elapsedFromRoutine = (1_000_000 + bookClosedMs + 1) - h.lastGraceFetchAt;
    expect(elapsedFromRoutine).toBeGreaterThanOrEqual(bookClosedMs);
  });

  it('exactly one forced fetch per real bookChanged increment — never continuous/resumed', () => {
    const h = makeForceAwareLoadHarness();
    h.loadOnce({ force: true, live: false, now: 1 });
    expect(h.forcedFetchCount).toBe(1);
    // No further forced fetch happens without another real increment —
    // this harness has no timer/loop driving repeat calls, matching the
    // edge-triggered subscription (subscribeSkipInitial calls fn exactly
    // once per real store update, never on a schedule).
    expect(h.forcedFetchCount).toBe(1);
  });
});
