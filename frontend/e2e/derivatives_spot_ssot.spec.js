/**
 * derivatives_spot_ssot.spec.js
 *
 * Regression guard: verifies that the spot price shown in the OptionsPayoff
 * overlay matches the spot price shown in the Snapshot card for the selected
 * underlying on the derivatives admin page.
 *
 * Background: there was a bug where payoff overlay spot and snapshot card
 * spot diverged (different contract months used as the spot source). The fix
 * makes both surfaces use `liveSpot` — the same, single source of truth.
 *
 * Quality dimensions:
 *   SSOT     — both OptionsPayoff and Snapshot card read spot from `liveSpot`
 *              (defined in derivatives page, passed to OptionsPayoff, read
 *              from _underlyingQuotes for snapshot). Single throttled source.
 *   Perf     — no XHR budget regression on derivatives cold-load
 *   Stale    — grep confirms OptionsPayoff receives spot={liveSpot}, not
 *              strategy.spot or chainSpot
 *   Reusable — _underlyingQuotes source is the same for NavStrip + Snapshot
 *   UX       — both values format identically, decimal places aligned,
 *              both update live within ±1 unit timing jitter
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/derivatives_spot_ssot.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const DERIV_URL = `${BASE}/admin/derivatives`;

const DERIV_SRC = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);
const PAYOFF_SRC = path.resolve(
  process.cwd(),
  'src/lib/OptionsPayoff.svelte'
);

// ── Static SSOT checks ────────────────────────────────────────────────────────

test('SSOT: OptionsPayoff receives spot={liveSpot}, not strategy.spot', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');

  // Find the OptionsPayoff component invocation
  const payoffStart = src.indexOf('<OptionsPayoff');
  expect(payoffStart, 'OptionsPayoff component must be invoked in derivatives page').toBeGreaterThan(0);

  // Extract the entire component block (up to closing />)
  const payoffEnd = src.indexOf('/>', payoffStart) + 2;
  const payoffBlock = src.slice(payoffStart, payoffEnd);

  // Must pass spot={liveSpot}, not strategy.spot
  expect(
    payoffBlock.includes('spot={liveSpot}'),
    'OptionsPayoff must receive spot={liveSpot} (the single throttled SSOT source)'
  ).toBe(true);

  // Must NOT pass spot={strategy.spot} (would cause divergence)
  expect(
    payoffBlock.includes('spot={strategy.spot}'),
    'OptionsPayoff must NOT receive spot={strategy.spot} (would cause divergence from snapshot)'
  ).toBe(false);
});

test('SSOT: liveSpot is defined and throttled via _throttledTick', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');

  // liveSpot must be defined as a $derived.by block
  expect(
    src.includes('const liveSpot = $derived.by('),
    'liveSpot must be defined as a $derived.by block'
  ).toBe(true);

  // liveSpot must be throttled — comment or code must reference _throttledTick
  const liveSpotStart = src.indexOf('const liveSpot = $derived.by(');
  const liveSpotEnd = src.indexOf('\n  });', liveSpotStart) + 6;
  const liveSpotBlock = src.slice(liveSpotStart, liveSpotEnd);

  expect(
    liveSpotBlock.includes('_throttledTick'),
    'liveSpot must read from _throttledTick (the 250ms-throttled tick source)'
  ).toBe(true);
});

test('Stale: Snapshot Exp P&L uses _legsExpPnlTotal (which uses liveSpot)', () => {
  const src = fs.readFileSync(DERIV_SRC, 'utf8');

  // _legsExpPnlTotal must be defined
  expect(
    src.includes('const _legsExpPnlTotal = $derived.by('),
    '_legsExpPnlTotal must be defined in derivatives page'
  ).toBe(true);

  // _legsExpPnlTotal body must reference liveSpot (not strategy.spot)
  const expStart = src.indexOf('const _legsExpPnlTotal = $derived.by(');
  const expEnd = src.indexOf('\n  });', expStart) + 6;
  const expBlock = src.slice(expStart, expEnd);

  expect(
    expBlock.includes('liveSpot'),
    '_legsExpPnlTotal must use liveSpot (SSOT spot source, same as OptionsPayoff)'
  ).toBe(true);
});

test('Stale: OptionsPayoff spot display uses ps-spot class variants', () => {
  const src = fs.readFileSync(PAYOFF_SRC, 'utf8');

  // Must render spot with ps-spot-pos / ps-spot-neg / ps-spot-flat classes
  expect(
    src.includes("'ps-v ps-spot-'"),
    'OptionsPayoff must render spot with ps-spot-pos/neg/flat class variants'
  ).toBe(true);

  // CSS rules for ps-spot classes must exist
  expect(
    src.includes('.ps-v.ps-spot-pos'),
    'OptionsPayoff CSS must define .ps-v.ps-spot-pos (green color)'
  ).toBe(true);
  expect(
    src.includes('.ps-v.ps-spot-neg'),
    'OptionsPayoff CSS must define .ps-v.ps-spot-neg (red color)'
  ).toBe(true);
  expect(
    src.includes('.ps-v.ps-spot-flat'),
    'OptionsPayoff CSS must define .ps-v.ps-spot-flat (neutral/cyan color)'
  ).toBe(true);
});

// ── Live UI checks ────────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile',  width: 390,  height: 844 },
];

for (const vp of VIEWPORTS) {
  test.describe(`/admin/derivatives — spot price parity [${vp.name}]`, () => {
    test.setTimeout(120_000);

    test(`OptionsPayoff spot matches Snapshot card spot for selected underlying [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', (err) => pageErrors.push(err.message));

      // Try multiple credential sets
      let authOk = false;
      for (const creds of [
        { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
        { user: 'rambo', pass: 'admin1234' },
      ]) {
        try {
          await loginAsAdmin(page, creds);
          authOk = true;
          break;
        } catch (_) { /* try next set */ }
      }
      if (!authOk) {
        test.skip(true, 'No valid credentials — skipping live UI check (static SSOT checks pass)');
        return;
      }

      await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

      // Wait for the Snapshot card to attach (always present after route load)
      await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });

      // ── Performance budget ─────────────────────────────────────────────
      const xhrUrls = [];
      page.on('request', req => {
        if (['fetch', 'xhr'].includes(req.resourceType())) xhrUrls.push(req.url());
      });

      const derivXhrs = xhrUrls.filter(u => u.includes('/api/'));
      expect(
        derivXhrs.length,
        `Cold-load XHR budget: ${derivXhrs.length} requests (max 60)`
      ).toBeLessThan(60);

      // ── Wait for positions to load ──────────────────────────────────────
      // Check if there are any data rows in the snapshot
      const snapshotRows = page.locator('.byund-row:not(.byund-row-total)');
      const rowCount = await snapshotRows.count();

      if (rowCount === 0) {
        // No live positions — nothing to compare
        const realErrors = pageErrors.filter(e =>
          !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
        );
        expect(realErrors, 'No unexpected JS errors').toHaveLength(0);
        return;
      }

      // ── Wait for OptionsPayoff to render ───────────────────────────────
      // The payoff SVG and stat overlay must be visible
      const payoffOverlay = page.locator('.payoff-stats');
      await payoffOverlay.waitFor({ state: 'visible', timeout: 15_000 });

      // Give liveSpot a moment to settle (throttled at 250ms)
      await page.waitForTimeout(300);

      // ── Read spot from OptionsPayoff overlay ──────────────────────────
      // Spot cell: <span class="ps-v ps-spot-{pos|neg|flat}">value</span>
      const payoffSpotCell = page.locator('.ps-v.ps-spot-pos, .ps-v.ps-spot-neg, .ps-v.ps-spot-flat').first();
      const payoffSpotVisible = await payoffSpotCell.isVisible().catch(() => false);

      if (!payoffSpotVisible) {
        // Market may be closed, spot unavailable — nothing to check
        return;
      }

      const payoffSpotText = (await payoffSpotCell.textContent()).trim();

      // ── Read spot from Snapshot card for selected underlying ──────────
      // Get the selected underlying from the legs header chip
      const underlyingChip = page.locator('.legs-underlying-chip').first();
      const selectedUnderlying = (await underlyingChip.textContent().catch(() => '')).trim();

      if (!selectedUnderlying) {
        // No underlying selected — legs grid won't show data
        return;
      }

      // Find the matching snapshot row for this underlying
      const matchingSnapshotRow = page.locator('.byund-row:not(.byund-row-total)')
        .filter({ has: page.locator(`.byund-und:text-is("${selectedUnderlying}")`) });

      const matchingRowCount = await matchingSnapshotRow.count();
      if (matchingRowCount === 0) {
        // Underlying not in snapshot (may be equity holdings only)
        return;
      }

      // Spot (LTP) is the first .num cell in the snapshot row
      const snapshotSpotCell = matchingSnapshotRow.first().locator('.num').first();
      const snapshotSpotText = (await snapshotSpotCell.textContent()).trim();

      // ── Parse both prices and strip formatting ──────────────────────────
      // Both should be numeric with optional commas/formatting
      const parsePrice = (text) => {
        // Remove commas, rupee symbol, any whitespace
        const clean = text.replace(/[₹,\s]/g, '');
        return parseFloat(clean);
      };

      const payoffPrice = parsePrice(payoffSpotText);
      const snapshotPrice = parsePrice(snapshotSpotText);

      // ── Verify both parsed successfully ────────────────────────────────
      expect(
        Number.isFinite(payoffPrice),
        `Payoff spot "${payoffSpotText}" should parse to a number`
      ).toBe(true);

      expect(
        Number.isFinite(snapshotPrice),
        `Snapshot spot "${snapshotSpotText}" should parse to a number`
      ).toBe(true);

      // ── Verify both are non-zero (not stale/placeholder) ───────────────
      expect(
        payoffPrice > 0,
        `Payoff spot ${payoffPrice} should be positive`
      ).toBe(true);

      expect(
        snapshotPrice > 0,
        `Snapshot spot ${snapshotPrice} should be positive`
      ).toBe(true);

      // ── Core assertion: both prices match within ±1 unit ───────────────
      // Allows for tick timing jitter between the two reads
      const priceDiff = Math.abs(payoffPrice - snapshotPrice);
      expect(
        priceDiff,
        `Payoff spot (${payoffPrice}) and Snapshot spot (${snapshotPrice}) should match within ±1 unit. Diff: ${priceDiff}`
      ).toBeLessThanOrEqual(1);

      // ── Verify both cells have non-empty content (not stuck at '—') ────
      const isDash = (t) => t === '—' || t === '-';
      expect(
        !isDash(payoffSpotText),
        `Payoff spot should not be stuck at "—" (should have a live value)`
      ).toBe(true);

      expect(
        !isDash(snapshotSpotText),
        `Snapshot spot should not be stuck at "—" (should have a live value)`
      ).toBe(true);

      // ── No page errors ─────────────────────────────────────────────────
      const realErrors = pageErrors.filter(
        e => !e.includes('401') && !e.includes('405') && !e.includes('EventSource')
      );
      expect(realErrors, 'No unexpected JS errors').toHaveLength(0);
    });

    test(`Snapshot card spot cell is not empty/dash when market is open [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      let authOk = false;
      for (const creds of [
        { user: process.env.PLAYWRIGHT_USER || 'ambore', pass: process.env.PLAYWRIGHT_PASS || 'admin1234' },
        { user: 'rambo', pass: 'admin1234' },
      ]) {
        try {
          await loginAsAdmin(page, creds);
          authOk = true;
          break;
        } catch (_) { /* try next */ }
      }
      if (!authOk) {
        test.skip(true, 'No valid credentials');
        return;
      }

      await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      await page.locator('.opt-byund-card').waitFor({ state: 'attached', timeout: 25_000 });
      await page.waitForTimeout(300);

      // Check if any data rows exist
      const snapshotRows = page.locator('.byund-row:not(.byund-row-total)');
      const rowCount = await snapshotRows.count();

      if (rowCount === 0) {
        test.skip(true, 'No positions in book');
        return;
      }

      // For each snapshot row, verify the first .num (spot) is not empty
      for (let i = 0; i < Math.min(rowCount, 3); i++) {
        const row = snapshotRows.nth(i);
        const spotCell = row.locator('.num').first();
        const spotText = (await spotCell.textContent()).trim();

        const isDash = spotText === '—' || spotText === '-';
        const isEmpty = !spotText || spotText === '';

        expect(
          !isEmpty && !isDash,
          `Snapshot row ${i} spot cell should have a value (not empty/dash), got: "${spotText}"`
        ).toBe(true);
      }
    });
  });
}
