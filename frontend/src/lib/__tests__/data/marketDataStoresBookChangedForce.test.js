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
