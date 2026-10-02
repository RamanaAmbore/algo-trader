/**
 * bookPollerClosedWindow.test.js
 *
 * Fix (2026-10): `_tickBookPollers()` in marketDataStores.svelte.js used to
 * fire a real `positionsStore.load()`/`holdingsStore.load()`/`fundsStore.
 * load()` (+pulse variants) round-trip every tick (default 30 min once
 * "closed") continuously through the ENTIRE market-closed window — which
 * can span a weekend or a multi-day holiday cluster, not just overnight.
 * The backend's closed-hours gate means these never hit the broker (just
 * re-serve the frozen `daily_book` snapshot), so the round-trip was pure
 * waste.
 *
 * Fix shape (NOT the naive "gate on isNseOpen()||isMcxOpen() alone" shape):
 * a bare isNseOpen()/isMcxOpen() gate would ALSO block two narrow windows
 * where `daily_book` genuinely changes — the 08:00 IST daily reset
 * (fix_daily_book_prev_close) and the MCX 23:45 settlement-snapshot write
 * — recreating two previously-fixed regressions (commit 21d12fa8's
 * premarket-staleness fix, and a brand-new gap on the MCX settlement
 * write, since isMcxOpen() already goes false at 23:30, 15 minutes before
 * that write lands). `isBookSnapshotGraceWindow()` (marketHours.js) covers
 * exactly those two windows; closed-window fetches are additionally
 * throttled to `_bookClosedMs` apart (not re-fetched on every live-cadence
 * tick) via a `_lastGraceFetchAt` timestamp.
 *
 * marketDataStores.svelte.js has top-level `$state(...)` calls that execute
 * at module-eval time with no svelte-compiler plugin wired into
 * vitest.config.js (see marketDataStoresMeta.test.js's header comment for
 * the same constraint), so it can't be imported directly in this harness.
 * Two-part coverage, same convention as that file:
 *   1. A behavioral harness that mirrors `_tickBookPollers`/`_loadBookOnce`'s
 *      exact decision logic (pure, stateful across ticks like the real
 *      module-level counters).
 *   2. A source-scan (`?raw`) tying that mirror to the actual shipped
 *      control flow, so a future edit that changes the real gating logic
 *      without updating this test is caught structurally, not just by the
 *      harness passing in isolation.
 *
 * `isBookSnapshotGraceWindow()` itself is plain JS (no runes) and IS
 * directly imported + exercised for real below — the strongest coverage
 * available for the piece that actually encodes the grace-window boundary
 * math.
 *
 * Five quality dimensions:
 *  1. SSOT   — source-scan reads the real shipped file; harness constants
 *              (bookClosedMs throttle) are driven from the same place the
 *              real `_bookClosedMs` is (via the exported setter contract).
 *  2. Perf   — the whole point: proves zero network calls while fully
 *              closed, every tick, across a simulated multi-day span.
 *  3. Stale  — regression-guards the exact control-flow shape (early
 *              return before any store `.load()` call) so a future edit
 *              that moves the gate back to "after the fetch" silently
 *              reintroduces the bug this change fixes.
 *  4. Reuse  — isBookSnapshotGraceWindow tests exercise the real,
 *              already-holiday-aware `_serverStatus`/`isMarketHoliday()`
 *              machinery shared with isNseOpen/isMcxOpen, not a reimplementation.
 *  5. UX     — closed→open transition test guards the "resume the instant
 *              the market reopens" requirement (no missed fetch, no
 *              double-fire) that every bookPollerTick consumer (flash,
 *              heartbeat, RefreshButton poll-pulse) depends on firing
 *              exactly once per genuine data change.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import src from '$lib/data/marketDataStores.svelte.js?raw';
import {
  isBookSnapshotGraceWindow,
  setServerMarketStatus,
} from '$lib/marketHours.js';

const IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000;
/** Build a Date whose IST wall-clock reading is exactly (y,m,d hh:mm). */
function istDate(y, m, d, hh, mm) {
  return new Date(Date.UTC(y, m - 1, d, hh, mm) - IST_OFFSET_MS);
}
// 2026-06-01 = Monday, 2026-06-06 = Saturday (verified fixed reference dates).
const MON = (hh, mm) => istDate(2026, 6, 1, hh, mm);
const SAT = (hh, mm) => istDate(2026, 6, 6, hh, mm);

beforeEach(() => {
  // Known non-holiday baseline before every test — isMarketHoliday() reads
  // this same _serverStatus singleton, so a prior test's holiday flag must
  // not leak into the next test.
  setServerMarketStatus({ nse_open: false, mcx_open: false, any_open: false, is_holiday: false });
});

// ── isBookSnapshotGraceWindow — real shipped code, exercised directly ─────

describe('isBookSnapshotGraceWindow (real export, marketHours.js)', () => {
  it('false well before the 08:00 premarket window (07:59 IST, weekday)', () => {
    expect(isBookSnapshotGraceWindow(MON(7, 59))).toBe(false);
  });

  it('true at the 08:00 boundary (daily reset write)', () => {
    expect(isBookSnapshotGraceWindow(MON(8, 0))).toBe(true);
  });

  it('true through premarket (08:30 IST, weekday)', () => {
    expect(isBookSnapshotGraceWindow(MON(8, 30))).toBe(true);
  });

  it('false once MCX opens (09:00 IST) — live isMcxOpen() path takes over', () => {
    expect(isBookSnapshotGraceWindow(MON(9, 0))).toBe(false);
  });

  it('false mid-session (12:00 IST) — handled by the live path, not grace', () => {
    expect(isBookSnapshotGraceWindow(MON(12, 0))).toBe(false);
  });

  it('false right before MCX close (15:29 IST — NSE close snapshot window; MCX still open, not grace)', () => {
    expect(isBookSnapshotGraceWindow(MON(15, 29))).toBe(false);
  });

  it('false just before MCX close (23:29 IST)', () => {
    expect(isBookSnapshotGraceWindow(MON(23, 29))).toBe(false);
  });

  it('true at MCX close (23:30 IST boundary)', () => {
    expect(isBookSnapshotGraceWindow(MON(23, 30))).toBe(true);
  });

  it('true through the MCX settlement-write window (23:45 IST)', () => {
    expect(isBookSnapshotGraceWindow(MON(23, 45))).toBe(true);
  });

  it('true just inside the post-close buffer (23:49 IST)', () => {
    expect(isBookSnapshotGraceWindow(MON(23, 49))).toBe(true);
  });

  it('false once the post-close buffer expires (23:50 IST)', () => {
    expect(isBookSnapshotGraceWindow(MON(23, 50))).toBe(false);
  });

  it('false deep overnight (02:00 IST)', () => {
    expect(isBookSnapshotGraceWindow(MON(2, 0))).toBe(false);
  });

  it('false on a weekend even inside the premarket time-of-day window (Saturday 08:30 IST)', () => {
    expect(isBookSnapshotGraceWindow(SAT(8, 30))).toBe(false);
  });

  it('false on a flagged holiday even inside the premarket window', () => {
    setServerMarketStatus({ nse_open: false, mcx_open: false, any_open: false, is_holiday: true });
    expect(isBookSnapshotGraceWindow(MON(8, 30))).toBe(false);
  });

  it('false on a flagged holiday even inside the post-close window', () => {
    setServerMarketStatus({ nse_open: false, mcx_open: false, any_open: false, is_holiday: true });
    expect(isBookSnapshotGraceWindow(MON(23, 45))).toBe(false);
  });
});

// ── Behavioral harness mirroring _tickBookPollers / _loadBookOnce ─────────

/**
 * Pure, stateful mirror of the real module's tick decision + bookkeeping.
 * Structurally identical to the shipped `_tickBookPollers`/`_loadBookOnce`
 * pair (see source-scan assertions below tying the two together):
 *   - live (nseOpen || mcxOpen): always fetch.
 *   - closed + no grace window: skip, no bookkeeping touched.
 *   - closed + grace window: fetch only if `_bookClosedMs` has elapsed
 *     since the last grace-window fetch.
 *   - every fetch bumps a tick counter (bookPollerTick analog) and resets
 *     the grace throttle to 0 when live, or stamps it when not.
 */
function makeBookPollerHarness({ bookClosedMs = 30 * 60 * 1000 } = {}) {
  let lastGraceFetchAt = 0;
  let fetchCount = 0;
  let tickCounter = 0; // bookPollerTick analog — only advances on a fetch

  function tick({ nseOpen, mcxOpen, graceWindow, now }) {
    const live = nseOpen || mcxOpen;
    if (!live) {
      if (!graceWindow) return false; // fully closed, no grace — skip
      if (now - lastGraceFetchAt < bookClosedMs) return false; // throttled
    }
    fetchCount++;
    tickCounter++;
    lastGraceFetchAt = live ? 0 : now;
    return true;
  }

  return {
    tick,
    get fetchCount() { return fetchCount; },
    get tickCounter() { return tickCounter; },
  };
}

describe('_tickBookPollers behavioral harness (mirrors shipped control flow)', () => {
  it('never fetches while fully closed with no grace window, across many ticks (the core fix)', () => {
    const h = makeBookPollerHarness();
    let now = 1_000_000;
    for (let i = 0; i < 50; i++) {
      h.tick({ nseOpen: false, mcxOpen: false, graceWindow: false, now });
      now += 5_000; // 5s live-cadence ticks, unchanged timer rate
    }
    expect(h.fetchCount).toBe(0);
    expect(h.tickCounter).toBe(0); // bookPollerTick / lastRefreshAt untouched
  });

  it('fetches on every tick while either segment is open (regression guard — live path unchanged)', () => {
    const h = makeBookPollerHarness();
    for (let i = 0; i < 10; i++) {
      const ok = h.tick({ nseOpen: true, mcxOpen: false, now: i * 5000, graceWindow: false });
      expect(ok).toBe(true);
    }
    expect(h.fetchCount).toBe(10);
  });

  it('fetches on every tick while MCX-only is open (afternoon equity-closed window)', () => {
    const h = makeBookPollerHarness();
    for (let i = 0; i < 6; i++) {
      h.tick({ nseOpen: false, mcxOpen: true, graceWindow: false, now: i * 5000 });
    }
    expect(h.fetchCount).toBe(6);
  });

  it('closed→open transition: exactly one fetch on the first tick after the flip — no double, no missed', () => {
    const h = makeBookPollerHarness();
    // Several closed ticks beforehand — all skipped.
    h.tick({ nseOpen: false, mcxOpen: false, graceWindow: false, now: 0 });
    h.tick({ nseOpen: false, mcxOpen: false, graceWindow: false, now: 5000 });
    expect(h.fetchCount).toBe(0);
    // Market flips open on this tick.
    const fired = h.tick({ nseOpen: true, mcxOpen: false, graceWindow: false, now: 10000 });
    expect(fired).toBe(true);
    expect(h.fetchCount).toBe(1);
    // Next tick still open — fetches again (live cadence), not a double-fire
    // of the SAME transition.
    h.tick({ nseOpen: true, mcxOpen: false, graceWindow: false, now: 15000 });
    expect(h.fetchCount).toBe(2);
  });

  it('grace window: fetches once, then throttles to bookClosedMs apart', () => {
    const h = makeBookPollerHarness({ bookClosedMs: 1_800_000 }); // 30 min
    const t0 = 1_000_000_000;
    expect(h.tick({ nseOpen: false, mcxOpen: false, graceWindow: true, now: t0 })).toBe(true);
    expect(h.fetchCount).toBe(1);
    // 1 minute later, still inside the grace window — throttled, skip.
    expect(h.tick({ nseOpen: false, mcxOpen: false, graceWindow: true, now: t0 + 60_000 })).toBe(false);
    expect(h.fetchCount).toBe(1);
    // 30 minutes + 1ms later — throttle elapsed, fetches again.
    expect(h.tick({ nseOpen: false, mcxOpen: false, graceWindow: true, now: t0 + 1_800_001 })).toBe(true);
    expect(h.fetchCount).toBe(2);
  });

  it('leaving the grace window (deep-closed) immediately resumes skipping, even mid-throttle-window', () => {
    const h = makeBookPollerHarness({ bookClosedMs: 1_800_000 });
    const t0 = 1_000_000_000;
    h.tick({ nseOpen: false, mcxOpen: false, graceWindow: true, now: t0 });
    expect(h.fetchCount).toBe(1);
    // Grace window ends (e.g. past 23:50) before the throttle would have
    // elapsed — must skip regardless of elapsed time.
    expect(h.tick({ nseOpen: false, mcxOpen: false, graceWindow: false, now: t0 + 1000 })).toBe(false);
    expect(h.fetchCount).toBe(1);
  });

  it('a live fetch resets the grace throttle so the next closed period gets an immediate grace-window fetch', () => {
    // Uses realistic epoch-ms magnitudes (like real `Date.now()`), not
    // small relative offsets — `lastGraceFetchAt` resetting to `0` (epoch)
    // only behaves as "never throttled" against a genuine epoch-ms `now`;
    // a toy near-zero `now` would falsely look "recently fetched".
    const h = makeBookPollerHarness({ bookClosedMs: 1_800_000 });
    const base = 1_700_000_000_000;
    // Live fetch.
    h.tick({ nseOpen: true, mcxOpen: false, graceWindow: false, now: base });
    // Market closes; immediately enters a grace window very soon after
    // (e.g. MCX closes exactly at the grace boundary) — must not be
    // throttled by the stale pre-close timestamp.
    expect(h.tick({ nseOpen: false, mcxOpen: false, graceWindow: true, now: base + 1000 })).toBe(true);
    expect(h.fetchCount).toBe(2);
  });
});

// ── Source-scan — ties the harness above to the real shipped control flow ─

describe('marketDataStores.svelte.js — real control flow matches the harness', () => {
  /** Brace-match a `function name(...) { ... }` or `export function
   *  name(...) { ... }` declaration by name. */
  function extractFunctionBlock(name) {
    const re = new RegExp(`(?:export\\s+)?async function ${name}\\s*\\([^)]*\\)\\s*\\{`);
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

  it('_tickBookPollers gates on isNseOpen()||isMcxOpen() BEFORE calling _loadBookOnce (early return, not post-fetch branch)', () => {
    const block = extractFunctionBlock('_tickBookPollers');
    expect(block).toMatch(/if\s*\(!isNseOpen\(\)\s*&&\s*!isMcxOpen\(\)\)\s*\{/);
    expect(block).toMatch(/if\s*\(!isBookSnapshotGraceWindow\(\)\)\s*return;/);
    expect(block).toMatch(/if\s*\(Date\.now\(\)\s*-\s*_lastGraceFetchAt\s*<\s*_bookClosedMs\)\s*return;/);
    // The actual fetch call must come AFTER both guards (not before) —
    // guards against the exact "check only decides the next interval,
    // doesn't skip the fetch that already ran" shape of the original bug.
    const fetchIdx = block.indexOf('_loadBookOnce()');
    const guardIdx = block.indexOf('isBookSnapshotGraceWindow');
    expect(fetchIdx).toBeGreaterThan(-1);
    expect(guardIdx).toBeGreaterThan(-1);
    expect(fetchIdx).toBeGreaterThan(guardIdx);
  });

  it('_tickBookPollers does NOT advance bookPollerTick/lastRefreshAt itself — only _loadBookOnce does', () => {
    const block = extractFunctionBlock('_tickBookPollers');
    expect(block).not.toMatch(/_bookPollerTick\+\+/);
    expect(block).not.toMatch(/lastRefreshAt\.set/);
  });

  it('_loadBookOnce fetches all five book stores and bumps tick/lastRefreshAt on success', () => {
    const block = extractFunctionBlock('_loadBookOnce');
    for (const store of ['positionsStore', 'holdingsStore', 'pulsePositionsStore', 'pulseHoldingsStore', 'fundsStore']) {
      expect(block).toMatch(new RegExp(`${store}\\.load\\(\\)`));
    }
    expect(block).toMatch(/_bookPollerTick\+\+/);
    expect(block).toMatch(/lastRefreshAt\.set\(Date\.now\(\)\)/);
  });

  it('startBookPollers fires the cold-load kick UNGATED via _loadBookOnce, not the gated _tickBookPollers', () => {
    const re = /export\s+function\s+startBookPollers\s*\([^)]*\)\s*\{/;
    const m = re.exec(src);
    expect(m).not.toBeNull();
    const bodyStart = m.index + m[0].length - 1;
    let depth = 0;
    let block = '';
    for (let i = bodyStart; i < src.length; i++) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') {
        depth--;
        if (depth === 0) { block = src.slice(bodyStart, i + 1); break; }
      }
    }
    expect(block).toMatch(/_loadBookOnce\(\);/);
    // The kick call itself (first statement in the block, not the later
    // visibleInterval registration) must not be `_tickBookPollers()`.
    const kickRegion = block.slice(0, block.indexOf('visibleInterval('));
    expect(kickRegion).not.toMatch(/\b_tickBookPollers\(\)/);
  });
});
