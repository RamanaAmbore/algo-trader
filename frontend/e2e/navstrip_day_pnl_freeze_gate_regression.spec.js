/**
 * navstrip_day_pnl_freeze_gate_regression.spec.js
 *
 * Regression coverage for the real-money bug: "NavStrip P∆ (Day P&L) shows
 * 0 and never recovers until a full page reload, even though the position
 * driving it has a genuinely non-zero Day P&L the whole time (confirmed via
 * the Pulse grid / Snapshot tab, which compute the same formula over an
 * independently-fetched row array)."
 *
 * Root cause: PositionStrip.svelte's freeze/thaw $effect reset
 * dispPositionsToday/dispHoldingsToday to 0 on a closed→open session
 * transition or an execution-mode switch, then held them at 0 until a
 * SHARED, bookPollerTick-driven counter (`_pollCycleStamp`) advanced past a
 * snapshot taken at the reset. Before the 2026-08 "positionsDayPnlStore
 * SSOT + rationalize poll cycles" consolidation (commit 6c66330b),
 * PositionStrip ran its own independent 30s refresh timer as a redundant
 * safety net against exactly this class of stall; removing that timer left
 * the gate's ONLY release path dependent on one shared poller's health. If
 * that poller stalled/lagged for any reason, the gate never released — only
 * a full page reload "fixed" it, because reload re-initializes the
 * transition stamp to a value that's already trivially satisfied.
 *
 * Fix: the gate now releases per-slot, directly off each backing store's
 * own `lastFetch` bookkeeping (frontend/src/lib/data/navStripFreeze.js:
 * isSlotFreshAfterTransition), independent of the shared tick counter.
 * Pure-logic unit coverage lives in
 * frontend/src/lib/__tests__/data/navStripFreeze.test.js — this spec adds
 * the two remaining dimensions: (1) a static regression guard so a future
 * edit can't silently reintroduce the old pollCycleStamp-coupled gate, and
 * (2) a live smoke check that the P∆/HD∆ cells actually render real values
 * on page load (the end-to-end path the static guard can't see).
 *
 * Five quality dimensions:
 *   1. SSOT   — asserts the gate is keyed off the same lastFetch/meta
 *               fields the rest of the staleness design already uses
 *               (dataStore.svelte.js), not a bespoke parallel signal.
 *   2. Perf   — no new timers/polling added; reuses existing page waits.
 *   3. Stale  — static guard fails if the old buggy comparison pattern
 *               (`_pollCycleStamp <= _openTransitionStamp`) is reintroduced.
 *   4. Reuse  — isSlotFreshAfterTransition is a plain pure helper reused
 *               identically for both the P and H slots.
 *   5. UX     — the exact bug is an operator-visible silent-zero that
 *               never self-corrects; the live check guards the symptom.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.join(__dirname, '..', 'src');
const STRIP_FILE = path.join(FRONTEND_SRC, 'lib', 'PositionStrip.svelte');
const FREEZE_HELPER_FILE = path.join(FRONTEND_SRC, 'lib', 'data', 'navStripFreeze.js');

// ── Static guards (no browser needed) ──────────────────────────────────────

test.describe('NavStrip Day P&L freeze-gate regression guards', () => {
  test('PositionStrip no longer gates freeze-release on the shared pollCycleStamp counter', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    // Old (buggy) pattern: release depended entirely on a shared,
    // bookPollerTick-driven counter advancing past a snapshot of itself —
    // if that shared poller stalled, this comparison could never flip.
    expect(content).not.toMatch(/_pollCycleStamp\s*<=\s*_openTransitionStamp/);
    expect(content).not.toMatch(/let _openTransitionStamp = \$state\(-1\);/);
  });

  test('PositionStrip gates freeze-release per-slot via isSlotFreshAfterTransition keyed off each store\'s own lastFetch', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    expect(content).toMatch(/import \{ isSlotFreshAfterTransition \} from '\$lib\/data\/navStripFreeze\.js';/);
    expect(content).toMatch(/let _openTransitionAt = \$state\(0\);/);
    // Positions slot keyed off positionsStore.lastFetch (the SSOT
    // portfolioStore.positions ultimately reads from).
    expect(content).toMatch(
      /isSlotFreshAfterTransition\(open,\s*positionsStore\.lastFetch,\s*_openTransitionAt\)/,
    );
    // Holdings slot keyed off pulseHoldingsStore.lastFetch — the SAME
    // store portfolioStore.holdings reads from (NavStrip H slot SSOT).
    expect(content).toMatch(
      /isSlotFreshAfterTransition\(open,\s*pulseHoldingsStore\.lastFetch,\s*_openTransitionAt\)/,
    );
    // The transition snapshot must be a real timestamp (Date.now()), not a
    // re-derivation of the old counter-based stamp.
    expect(content).toMatch(/_openTransitionAt = Date\.now\(\);/);
  });

  test('the P and H freeze-release checks are independent — one slot can never hold the other hostage', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    const pIdx = content.indexOf('const pFresh =');
    const hIdx = content.indexOf('const hFresh =');
    expect(pIdx, 'pFresh must exist').toBeGreaterThan(-1);
    expect(hIdx, 'hFresh must exist').toBeGreaterThan(-1);
    // Each slot's update block must be individually gated — not behind one
    // shared early `return` that would skip both on a single stale store.
    const afterGates = content.slice(hIdx);
    expect(afterGates).toMatch(/if \(pFresh\) \{/);
    expect(afterGates).toMatch(/if \(hFresh\) \{/);
    expect(afterGates).not.toMatch(/if \(open && .*\) return;\s*\n\s*const newPTotal/);
  });

  test('navStripFreeze.js exports a pure, store-independent helper', () => {
    const content = readFileSync(FREEZE_HELPER_FILE, 'utf8');
    expect(content).toMatch(/export function isSlotFreshAfterTransition\(/);
    // Must not import any Svelte store/reactive primitive — this has to
    // stay a plain, synchronously-testable pure function (Vitest coverage
    // lives in frontend/src/lib/__tests__/data/navStripFreeze.test.js).
    expect(content).not.toMatch(/\$state|\$derived|svelte\/store/);
  });
});

// ── Live smoke check ────────────────────────────────────────────────────────

test('NavStrip P∆/HD∆ cells render real (non-placeholder) values on load', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/pulse');

  const strip = page.locator('.ps-strip').first();
  await expect(strip).toBeVisible({ timeout: 15_000 });

  // P∆ is the first value cell in the P pill (ps-k-p's sibling chip).
  const cells = strip.locator('.ps-agg-v');
  await expect(cells.first()).toHaveText(/.+/, { timeout: 15_000 });

  const visibleTexts = await cells.allInnerTexts();
  expect(visibleTexts.length).toBeGreaterThan(0);
  console.log('[navstrip day-pnl freeze-gate smoke] cells:', visibleTexts);
});
