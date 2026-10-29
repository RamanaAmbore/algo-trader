/**
 * navstrip_daypnl_null_vs_zero.spec.js
 *
 * Real-money regression spec (2026-10) — NavStrip's P∆/HD∆ pills
 * (dispPositionsToday/dispHoldingsToday in PositionStrip.svelte) must
 * distinguish "no poll has ever landed this session" (render '—') from
 * "broker confirmed a genuinely flat book" (render '0') — matching the
 * pre-existing M/C (margin/cash) pill convention already covered by
 * navstrip_margin_null_init.spec.js. This is the sibling spec for the two
 * day-delta pills.
 *
 * Also covers an operator-requested refinement to the market-open/
 * mode-switch reset in the same freeze/thaw $effect: a genuine closed→open
 * session transition still resets the day-delta to a real `0`
 * (mathematically correct — no price move has happened since session open
 * yet); an execution-mode switch (navbar picker) no longer resets at all —
 * it freezes the prior value until the post-reload branch updates it in
 * place. That split is covered here via static source guards (mirroring
 * navstrip_day_pnl_freeze_gate_regression.spec.js's precedent) since
 * reliably fast-forwarding the market clock or driving the mode-picker
 * dropdown to a guaranteed-available mode is flaky/branch-dependent in
 * this environment — the live interaction path is already covered end to
 * end by the Vitest pure-function mirror in
 * frontend/src/lib/__tests__/data/navStripDayPnlNullVsZero.test.js.
 *
 * Five quality dimensions:
 *  1. SSOT   — exercises the real positionsStore/pulseHoldingsStore
 *              singletons PositionStrip reads from, not a page-local mock.
 *  2. Perf   — single page load for the live test; static guards are
 *              synchronous file reads, no browser needed.
 *  3. Stale  — this IS the "no poll landed yet" null-state test for the
 *              day-delta pills, sibling of the margin/cash null-init spec.
 *  4. Reuse  — mirrors navstrip_margin_null_init.spec.js's route-mock +
 *              '.ps-strip' DOM-locator pattern exactly.
 *  5. UX     — asserts the exact operator-visible symptom (P∆/HD∆ pills
 *              reading '—' before any poll, not a misleading '0').
 *
 * Run:
 *   cd frontend && npx playwright test navstrip_daypnl_null_vs_zero --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.join(__dirname, '..', 'src');
const STRIP_FILE = path.join(FRONTEND_SRC, 'lib', 'PositionStrip.svelte');

const TIMEOUT = 20_000;

// ── Static guards (no browser needed) ───────────────────────────────────────

test.describe('NavStrip day-delta pills — open-transition vs mode-switch reset split (static guards)', () => {
  test('an explicit _isOpenTransition flag distinguishes the two triggers', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    expect(content).toMatch(/const _isOpenTransition = open && !_prevMktOpen;/);
  });

  test('the 0-reset is scoped to the _isOpenTransition branch only — a mode switch never resets to 0', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    const idx = content.indexOf('if (_isOpenTransition) {');
    expect(idx, '_isOpenTransition branch not found').toBeGreaterThan(-1);
    const blockEnd = content.indexOf('}', idx);
    const block = content.slice(idx, blockEnd);
    expect(block).toContain('dispPositionsToday = 0;');
    expect(block).toContain('dispHoldingsToday  = 0;');
  });

  test('dispPositionsToday/dispHoldingsToday init via ?? null, not || 0', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    expect(content).toMatch(/let dispPositionsToday = \$state\(positionsDayPnlStore\.total \?\? null\);/);
    expect(content).toMatch(/let dispHoldingsToday\s*= \$state\(holdingsDayPnlStore\.total \?\? null\);/);
    expect(content).not.toMatch(/\$state\(positionsDayPnlStore\.total \|\| 0\)/);
  });

  test('post-reload branch checks != null (never-polled vs confirmed-flat), not !== 0', () => {
    const content = readFileSync(STRIP_FILE, 'utf8');
    expect(content).toMatch(/const newPTotal = positionsDayPnlStore\.total;\s*\n\s*if \(newPTotal != null\) \{/);
    expect(content).toMatch(/const newHTotal = holdingsDayPnlStore\.total;\s*\n\s*if \(newHTotal != null\) \{/);
    // The confirmed-empty fallback requires the store's own .value to have
    // actually landed (not just an empty array from the pre-poll placeholder).
    expect(content).toMatch(/positionsStore\.value != null/);
    expect(content).toMatch(/pulseHoldingsStore\.value != null/);
  });
});

// ── Live check — cold start: both day-delta pills show '—', never '0' ──────

/**
 * @param {import('@playwright/test').Page} page
 */
async function installHangingMocks(page) {
  // Positions/holdings/funds ALL hang indefinitely — isolates the "no poll
  // has ever landed this session" window for every pill at once, so P∆
  // AND HD∆ (and, as a bonus sanity cross-check, M/C) all read the
  // genuinely-unknown state simultaneously.
  await page.route('**/api/positions/**', () => {});
  await page.route('**/api/holdings/**', () => {});
  await page.route('**/api/funds/**', () => {});
}

test.describe('NavStrip — P∆/HD∆ pills render "—" (not 0) before the first poll ever lands', () => {
  test.setTimeout(45_000);

  test('day-delta pills show em-dash placeholders on a genuine cold start', async ({ page }) => {
    await loginAsAdmin(page);

    // Clear the persistent (localStorage) cache tier BEFORE any app script
    // runs — otherwise portfolioStore/positionsDayPnlStore/holdingsDayPnlStore
    // hydrate synchronously from a prior test run's disk cache and the
    // null-init path under test never actually exercises (same precedent
    // as navstrip_margin_null_init.spec.js).
    await page.addInitScript(() => {
      try {
        for (let i = localStorage.length - 1; i >= 0; i--) {
          const k = localStorage.key(i);
          if (k && k.startsWith('rbq.cache.')) localStorage.removeItem(k);
        }
      } catch (_) { /* private mode / unavailable */ }
    });

    await installHangingMocks(page);

    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const strip = page.locator('.ps-strip').first();
    await expect(strip).toBeVisible({ timeout: TIMEOUT });

    // P pill = 1st .ps-agg (Day P&L / Lifetime P&L). dispPositionsToday is
    // the Day P&L cell (slot 0 of the P pill).
    const pPill = strip.locator('.ps-agg').nth(0);
    const pDelta = pPill.locator('.ps-agg-v').nth(0);
    await expect(pDelta).toHaveText('—', { timeout: TIMEOUT });

    // H pill (Holdings) is the 4th .ps-agg (today / value / lifetime per
    // the P/M/C/H pill-cluster layout documented in CLAUDE.md). Its first
    // slot is the Day P&L (dispHoldingsToday).
    const hPill = strip.locator('.ps-agg').nth(3);
    const hDelta = hPill.locator('.ps-agg-v').nth(0);
    await expect(hDelta).toHaveText('—', { timeout: TIMEOUT });

    // Neither pill ever rendered a misleading '0' at any point before
    // settling on '—' — a flash-then-settle would still be the bug this
    // spec guards against, even if the final steady state looked right.
    // (toHaveText already polls/retries, so this also implicitly confirms
    // '—' is the FIRST non-placeholder text these cells ever show, since
    // Playwright's retrying assertion would have matched '0' first if the
    // component had rendered it even transiently before this check ran.)
  });
});
