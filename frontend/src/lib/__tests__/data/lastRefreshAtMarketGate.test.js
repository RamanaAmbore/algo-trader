/**
 * lastRefreshAtMarketGate.test.js
 *
 * Fix (2026-10): `lastRefreshAt` (frontend/src/lib/stores.js), the header
 * "last refreshed" clock rendered by AlgoTimestamp.svelte on every algo
 * page, used to be stamped with `Date.now()` on EVERY completed fetch
 * cycle at four call sites — regardless of whether the market was open.
 * During the closed window, background pollers keep running and keep
 * re-serving the SAME frozen snapshot data, but the clock kept advancing
 * every cycle, falsely implying fresh data had arrived. Operator: "the
 * ltp refresh time also should be frozen when no ltp polling and
 * websocket during closing does not happen."
 *
 * Fix shape: a FREEZE, not a reset — every `lastRefreshAt.set(Date.now())`
 * call is now gated on `isNseOpen() || isMcxOpen()` (same AND-gate shape
 * as RefreshButton.svelte's own palette logic and
 * tickStaleness.svelte.js's `_check()`). When closed, the `.set()` call is
 * simply skipped — the store keeps whatever value it already held.
 *
 * This is a source-scan (`?raw`) regression guard over all four call
 * sites — the strongest coverage available for RefreshButton.svelte /
 * the two route files, since this harness has no svelte-compiler plugin
 * wired in (same documented constraint as marketDataStoresMeta.test.js /
 * bookPollerClosedWindow.test.js, which this file complements —
 * marketDataStores.svelte.js's own `_loadBookOnce` gate is covered there,
 * not duplicated here).
 *
 * Five quality dimensions:
 *  1. SSOT   — every site reuses isNseOpen()/isMcxOpen() from
 *              $lib/marketHours, never a reimplementation or a different
 *              predicate (e.g. isMarketOpen()'s any_open, a distinct
 *              signal).
 *  2. Perf   — n/a (no new network calls; pure gating of an existing
 *              store write).
 *  3. Stale  — regression-guards the exact "skip the .set() entirely"
 *              shape (freeze) against a future edit that resets the
 *              store to 0/null instead (which would blank the header
 *              clock during closed hours — a "never blank during closed
 *              hours" violation).
 *  4. Reuse  — all four sites reuse the same isNseOpen/isMcxOpen pair,
 *              not a bespoke per-file market-open check.
 *  5. UX     — the header clock must never advance on a closed-market
 *              snapshot re-serve, so an operator never mistakes stale
 *              data for a fresh poll.
 */

import { describe, it, expect } from 'vitest';
import refreshButtonSrc from '../../RefreshButton.svelte?raw';
import derivativesSrc from '../../../routes/(algo)/admin/derivatives/+page.svelte?raw';
import dashboardSrc from '../../../routes/(algo)/dashboard/+page.svelte?raw';

describe('RefreshButton.svelte — lastRefreshAt gated on market-open', () => {
  it('imports isNseOpen/isMcxOpen from $lib/marketHours', () => {
    expect(refreshButtonSrc).toMatch(
      /import\s*\{\s*isNseOpen,\s*isMcxOpen\s*\}\s*from\s*'\$lib\/marketHours'/
    );
  });

  it('the loading-transition effect only stamps lastRefreshAt when a segment is open', () => {
    expect(refreshButtonSrc).toMatch(
      /if\s*\(_prevLoading\s*&&\s*!loading\s*&&\s*\(_nseOpen\s*\|\|\s*_mcxOpen\)\)\s*\{\s*\n\s*lastRefreshAt\.set\(Date\.now\(\)\);/
    );
  });

  it('the post-hibernation refire effect only stamps lastRefreshAt when a segment is open', () => {
    expect(refreshButtonSrc).toMatch(
      /if\s*\(_prevRefiring\s*&&\s*!_refiring\s*&&\s*\(_nseOpen\s*\|\|\s*_mcxOpen\)\)\s*lastRefreshAt\.set\(Date\.now\(\)\);/
    );
  });
});

describe('derivatives +page.svelte — loadPositions() lastRefreshAt gate', () => {
  it('imports isNseOpen/isMcxOpen alongside the pre-existing isMarketOpen import', () => {
    expect(derivativesSrc).toMatch(
      /import\s*\{\s*isMarketOpen,\s*isNseOpen,\s*isMcxOpen\s*\}\s*from\s*'\$lib\/marketHours'/
    );
  });

  it('keeps the exact pre-existing literal statement other specs source-scan for, now inside a market-open guard', () => {
    // Two other e2e specs (derivatives_reactive_chain.spec.js,
    // exp_pnl_edge_cases.spec.js) do a literal indexOf() lookup for this
    // exact substring inside loadPositions() — the fix MUST wrap it in an
    // outer guard rather than rewriting the condition itself, or those
    // specs break.
    const literal = 'if (!positionsStore.error) lastRefreshAt.set(Date.now());';
    expect(derivativesSrc).toContain(literal);
    const idx = derivativesSrc.indexOf(literal);
    // Walk backward from the literal to the nearest preceding `if (` to
    // confirm it is nested inside the market-open guard, not bare at the
    // same level loadPositions() left it before this fix.
    const guardIdx = derivativesSrc.lastIndexOf('if (isNseOpen() || isMcxOpen())', idx);
    expect(guardIdx).toBeGreaterThan(-1);
    expect(guardIdx).toBeLessThan(idx);
    // No unrelated `if (`/`}` pair must close the guard before the
    // literal — the guard's own opening brace must be the nearest `{`
    // between guardIdx and idx.
    const between = derivativesSrc.slice(guardIdx, idx);
    expect(between).toMatch(/if\s*\(isNseOpen\(\)\s*\|\|\s*isMcxOpen\(\)\)\s*\{/);
    expect(between.indexOf('}')).toBe(-1); // guard body not closed before the literal
  });
});

describe('dashboard +page.svelte — hero-batch lastRefreshAt gate', () => {
  it('imports isNseOpen/isMcxOpen from $lib/marketHours', () => {
    expect(dashboardSrc).toMatch(
      /import\s*\{\s*isNseOpen,\s*isMcxOpen\s*\}\s*from\s*'\$lib\/marketHours'/
    );
  });

  it('gates the hero-batch lastRefreshAt stamp on market-open', () => {
    expect(dashboardSrc).toMatch(
      /if\s*\(isNseOpen\(\)\s*\|\|\s*isMcxOpen\(\)\)\s*lastRefreshAt\.set\(Date\.now\(\)\);/
    );
  });
});
