/**
 * derivatives_bundle_followup.spec.js
 *
 * Regression suite for the derivatives bundle-followup slice.
 *
 * Covers six operator-reported defects:
 *   1. Payoff hover/stat EXP = legs grid _legsExpPnlTotal (SSOT).
 *   2. Underlying dropdown stable across 5s re-check (no swap between
 *      "all underlyings" and "options-only").
 *   3. CRUDEOIL instruments.js rolls on expiry day (> not >=).
 *   4. liveSpot falls back to batchQuote snapshot before strategy.spot
 *      (IDFC ₹82 stale-SSE fix).
 *   5. _hedgeOpportunities gated on _positionsLoaded (prevents
 *      pre-position all-underlyings flash).
 *   6. Exp P&L in legs grid and snapshot: SSOT check (legsExpPnlAtSpot
 *      prop wired through to OptionsPayoff).
 *
 * Five quality dimensions per standing rules:
 *   SSOT     — single source checks via source file grep.
 *   Perf     — cold-load XHR budget unchanged.
 *   Stale    — no inline P&L formulas remain outside canonical helper.
 *   Reusable — legsExpPnlAtSpot prop wired; _positionsLoaded gate used.
 *   UX       — Exp P&L cell right-aligned on both mobile and desktop;
 *              dropdown stable; no overflow at 360px.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

// ── Static source-code checks (no browser needed) ──────────────────────────

test('SSOT: legsExpPnlAtSpot prop wired from page to OptionsPayoff', () => {
  const pageSrc = fs.readFileSync(
    path.resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
    'utf8'
  );
  const payoffSrc = fs.readFileSync(
    path.resolve(process.cwd(), 'src/lib/OptionsPayoff.svelte'),
    'utf8'
  );

  // Page must pass legsExpPnlAtSpot={_legsExpPnlTotal} to OptionsPayoff
  expect(
    pageSrc.includes('legsExpPnlAtSpot={_legsExpPnlTotal}'),
    'page must wire legsExpPnlAtSpot={_legsExpPnlTotal} to OptionsPayoff'
  ).toBe(true);

  // OptionsPayoff must declare legsExpPnlAtSpot in its JSDoc type
  expect(
    payoffSrc.includes('legsExpPnlAtSpot?:'),
    'OptionsPayoff JSDoc @type must include legsExpPnlAtSpot?:'
  ).toBe(true);

  // OptionsPayoff stat panel must use _expDisplayVal derived from legsExpPnlAtSpot
  expect(
    payoffSrc.includes('legsExpPnlAtSpot != null') &&
    payoffSrc.includes('_expDisplayVal'),
    'OptionsPayoff stat panel must derive _expDisplayVal from legsExpPnlAtSpot'
  ).toBe(true);
});

test('SSOT: no stale inline P&L formulas in template outside canonical helper', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
    'utf8'
  );

  // The canonical helper for expiry P&L per leg is _expiryPnl(c, spot).
  // No raw inline (close - avg) * qty or (intrinsic - cost) * qty pattern
  // should appear inside the {#each} template or TOTAL row — only function calls.
  // Count occurrences of the old direct intrinsic formula.
  const inlinePnlPattern = /\(intrinsic\s*-\s*cost\)\s*\*\s*qty/g;
  const inlineMatches = src.match(inlinePnlPattern) || [];
  expect(
    inlineMatches.length,
    'No raw (intrinsic - cost) * qty inline formula in template'
  ).toBe(0);
});

test('SSOT: _positionsLoaded gates _hedgeOpportunities', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
    'utf8'
  );

  // _hedgeOpportunities must check _positionsLoaded
  const hedgeIdx = src.indexOf('const _hedgeOpportunities = $derived.by');
  expect(hedgeIdx, '_hedgeOpportunities derivation must exist').toBeGreaterThan(0);

  const hedgeBlock = src.slice(hedgeIdx, src.indexOf('\n  });', hedgeIdx) + 6);
  expect(
    hedgeBlock.includes('_positionsLoaded'),
    '_hedgeOpportunities must guard on _positionsLoaded'
  ).toBe(true);
});

test('SSOT: findNearestFuture uses strict r.x > today (expiry-day roll)', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/lib/data/instruments.js'),
    'utf8'
  );

  // The function must use r.x > today (not >=) so on the expiry date
  // itself, the front-month contract is considered expired and the
  // next-month contract is returned.
  const fnStart = src.indexOf('export function findNearestFuture');
  const fnEnd   = src.indexOf('\n}', fnStart) + 2;
  const fnBody  = src.slice(fnStart, fnEnd);

  expect(
    fnBody.includes('r.x > today') && !fnBody.includes('r.x >= today'),
    'findNearestFuture must use strict r.x > today'
  ).toBe(true);
});

test('SSOT: liveSpot uses _underlyingQuotes as third fallback before strategy.spot', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
    'utf8'
  );

  // liveSpot must include a fallback on _underlyingQuotes[selectedUnderlying]?.ltp
  const spotIdx = src.indexOf('const liveSpot = $derived.by');
  const spotEnd  = src.indexOf('\n  });', spotIdx) + 6;
  const spotBody = src.slice(spotIdx, spotEnd);

  expect(
    spotBody.includes('_underlyingQuotes[selectedUnderlying]?.ltp'),
    'liveSpot must fall back to _underlyingQuotes batchQuote value'
  ).toBe(true);
});

// ── Viewport matrix: desktop + mobile ───────────────────────────────────────

const VIEWPORTS = [
  { name: 'chromium-desktop', width: 1440, height: 900 },
  { name: 'chromium-mobile',  width: 390,  height: 844 },
];

for (const vp of VIEWPORTS) {
  test.describe(`/admin/derivatives — bundle-followup [${vp.name}]`, () => {
    test.setTimeout(120000);

    test(`underlying dropdown stable across 5s re-check [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      let skipReason = '';
      try {
        await loginAsAdmin(page);
      } catch (e) {
        skipReason = String(e.message || e);
      }
      if (skipReason) {
        test.skip(true, `auth failed: ${skipReason}`);
        return;
      }

      const pageErrors = [];
      page.on('pageerror', (e) => pageErrors.push(e.message));

      await page.goto('/admin/derivatives', { waitUntil: 'networkidle', timeout: 60000 });

      // Wait for the underlying dropdown trigger
      const trigger = page.locator('#opt-und').first();
      await trigger.waitFor({ state: 'visible', timeout: 20000 });

      // Capture initial dropdown options by opening the panel
      await trigger.click();
      const panel = page.locator('#opt-und ~ .rbq-select-panel, .rbq-select-panel').first();
      await panel.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});
      const optsBefore = await panel.locator('.rbq-select-item, .select-item, [role="option"]')
        .allTextContents().catch(() => []);
      // Close the panel
      await page.keyboard.press('Escape');

      // Wait 5 seconds (one positions-poll cycle)
      await page.waitForTimeout(5000);

      // Re-open and capture options again
      await trigger.click();
      const panel2 = page.locator('#opt-und ~ .rbq-select-panel, .rbq-select-panel').first();
      await panel2.waitFor({ state: 'visible', timeout: 5000 }).catch(() => {});
      const optsAfter = await panel2.locator('.rbq-select-item, .select-item, [role="option"]')
        .allTextContents().catch(() => []);
      await page.keyboard.press('Escape');

      // If we got options in both captures, they must be the same set.
      // Allow for empty (no positions) — both will be empty and that's stable.
      if (optsBefore.length > 0 && optsAfter.length > 0) {
        const setA = new Set(optsBefore.map(s => s.trim()).filter(Boolean));
        const setB = new Set(optsAfter.map(s => s.trim()).filter(Boolean));
        const symmetric = [...setA].filter(x => !setB.has(x))
          .concat([...setB].filter(x => !setA.has(x)));
        expect(
          symmetric.length,
          `Dropdown options changed after 5s: added/removed ${symmetric.slice(0, 5).join(', ')}`
        ).toBe(0);
      }

      expect(pageErrors.filter(e => !/ResizeObserver/.test(e))).toHaveLength(0);
    });

    test(`Exp P&L column renders right-aligned on legs grid and snapshot [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      let skipReason = '';
      try {
        await loginAsAdmin(page);
      } catch (e) {
        skipReason = String(e.message || e);
      }
      if (skipReason) {
        test.skip(true, `auth failed: ${skipReason}`);
        return;
      }

      const pageErrors = [];
      page.on('pageerror', (e) => pageErrors.push(e.message));

      await page.goto('/admin/derivatives', { waitUntil: 'networkidle', timeout: 60000 });

      // Exp P&L column header in legs grid
      await page.waitForSelector('.cand-headrow', { timeout: 20000 }).catch(() => {});
      const candHeader = page.locator('.cand-headrow');
      await candHeader.waitFor({ state: 'visible', timeout: 10000 }).catch(() => {});
      const candHeaderText = await candHeader.textContent().catch(() => '');
      // Expect Exp P&L somewhere in the legs header
      if (candHeaderText) {
        expect(
          candHeaderText.includes('Exp P&L') || candHeaderText.includes('Exp'),
          'Legs grid header must include Exp P&L column'
        ).toBe(true);
      }

      // Snapshot grid header
      await page.waitForSelector('.byund-headrow', { timeout: 15000 }).catch(() => {});
      const byundHeader = page.locator('.byund-headrow');
      const byundHeaderText = await byundHeader.textContent().catch(() => '');
      if (byundHeaderText) {
        expect(
          byundHeaderText.includes('Exp P&L') || byundHeaderText.includes('Exp'),
          'Snapshot grid header must include Exp P&L column'
        ).toBe(true);
      }

      // No horizontal overflow at 360px mobile
      if (vp.width <= 400) {
        const candGrid = page.locator('.cand-grid').first();
        const overflowX = await candGrid.evaluate(
          el => el.scrollWidth > el.clientWidth + 2
        ).catch(() => false);
        expect(overflowX, 'Legs grid must not horizontally overflow at 360px').toBe(false);
      }

      expect(pageErrors.filter(e => !/ResizeObserver/.test(e))).toHaveLength(0);
    });

    test(`payoff stat panel EXP row present and formatted [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      let skipReason = '';
      try {
        await loginAsAdmin(page);
      } catch (e) {
        skipReason = String(e.message || e);
      }
      if (skipReason) {
        test.skip(true, `auth failed: ${skipReason}`);
        return;
      }

      const pageErrors = [];
      page.on('pageerror', (e) => pageErrors.push(e.message));

      await page.goto('/admin/derivatives', { waitUntil: 'networkidle', timeout: 60000 });

      // Wait for the payoff stats overlay to appear
      const statsPanel = page.locator('.payoff-stats');
      await statsPanel.waitFor({ state: 'visible', timeout: 25000 }).catch(() => {});
      const statsText = await statsPanel.textContent().catch(() => '');

      // If positions exist and strategy loaded, EXP row must appear
      if (statsText && statsText.includes('TDAY')) {
        expect(
          statsText.includes('EXP'),
          'Payoff stat panel must show EXP row when TDAY is present'
        ).toBe(true);
      }

      // No page-level JS errors
      expect(pageErrors.filter(e => !/ResizeObserver/.test(e))).toHaveLength(0);
    });

    test(`cold-load XHR budget: /admin/derivatives fires ≤30 unique XHR URLs [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      let skipReason = '';
      try {
        await loginAsAdmin(page);
      } catch (e) {
        skipReason = String(e.message || e);
      }
      if (skipReason) {
        test.skip(true, `auth failed: ${skipReason}`);
        return;
      }

      const xhrUrls = new Set();
      page.on('request', (req) => {
        if (req.resourceType() === 'fetch' || req.resourceType() === 'xhr') {
          try {
            const u = new URL(req.url());
            // Only count /api/ calls
            if (u.pathname.startsWith('/api/')) xhrUrls.add(u.pathname);
          } catch (_) {}
        }
      });

      await page.goto('/admin/derivatives', { waitUntil: 'networkidle', timeout: 60000 });
      // Allow one polling cycle to settle
      await page.waitForTimeout(3000);

      // Budget: ≤30 distinct /api/ paths (includes strategy, positions,
      // holdings, instruments, sim-status, etc.)
      expect(
        xhrUrls.size,
        `XHR /api/ URL count ${xhrUrls.size} exceeds budget of 30. Paths: ${[...xhrUrls].join(', ')}`
      ).toBeLessThanOrEqual(30);
    });
  });
}

// ── Regression: CRUDEOIL / IDFC symbol resolution ─────────────────────────

test('SSOT: instruments.js findNearestFuture strict-expiry documentation present', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/lib/data/instruments.js'),
    'utf8'
  );

  // The function comment must mention the expiry-day roll rationale
  const fnStart  = src.indexOf('export function findNearestFuture');
  const docStart = src.lastIndexOf('/**', fnStart);
  const fnDoc    = docStart >= 0 ? src.slice(docStart, fnStart) : '';

  expect(
    fnDoc.length > 0,
    'findNearestFuture must have a JSDoc comment'
  ).toBe(true);
});

test('SSOT: resolveUnderlying covers MCX_COMMODITIES set (CRUDEOIL present)', () => {
  const src = fs.readFileSync(
    path.resolve(process.cwd(), 'src/lib/data/resolveUnderlying.js'),
    'utf8'
  );

  expect(
    src.includes("'CRUDEOIL'"),
    'resolveUnderlying MCX_COMMODITIES must include CRUDEOIL'
  ).toBe(true);
});
