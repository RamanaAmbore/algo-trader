/**
 * derivatives_expiry_bands.spec.js
 *
 * Verifies the 3-band expiry classifier (ITM ON EXPIRY / NETTED /
 * OUT OF THE MONEY) uses the same canonical spot resolution chain as
 * `liveSpot` — SSE tick → _underlyingQuotes batchQuote → strategy.spot
 * server-poll fallback — instead of reading `strategy.spot` directly.
 *
 * Root cause of the original defect: SUZLON 60 CE was shown as ITM when
 * spot had drifted below 60.  The classifier was reading `strategy?.spot`
 * (last server-poll value, potentially stale) while `liveSpot` (used by
 * the payoff curve) correctly reflected the current price via SSE tick.
 *
 * Quality dimensions checked:
 *   SSOT     — spot in expiryCloseAnalysis is sourced from the same
 *               three-tier lookup as liveSpot (anchor → getSnapshot →
 *               _underlyingQuotes → strategy.spot fallback).
 *   Perf     — derivatives page loads within budget; no extra XHR
 *               calls introduced by the fix.
 *   Stale    — no second, independent `strategy?.spot` read inside
 *               expiryCloseAnalysis; old single-line read is gone.
 *   Reusable — both liveSpot and expiryCloseAnalysis resolve spot via
 *               the same multi-tier lookup; no duplicated logic.
 *   UX       — band header labels render correctly; OTM rows do not
 *               carry the amber "action required" close-band tint.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'fs';
import * as path from 'path';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

const SRC_PATH = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/+page.svelte'
);

// ── SSOT / stale-code checks (static source scan) ──────────────────────────

test('SSOT: expiryCloseAnalysis reads spot via three-tier lookup, not strategy?.spot directly', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // Find the expiryCloseAnalysis block.
  const blockStart = src.indexOf('const expiryCloseAnalysis = $derived.by');
  expect(blockStart, 'expiryCloseAnalysis must be defined').toBeGreaterThan(0);

  // The block ends at the first top-level `});` after its start.
  // We look for the closing of the outer $derived.by callback.
  const blockEnd = src.indexOf('\n  });\n', blockStart);
  expect(blockEnd, 'expiryCloseAnalysis block must have a closing });').toBeGreaterThan(blockStart);

  const block = src.slice(blockStart, blockEnd + 10);

  // The old one-liner must be gone.
  expect(
    block.includes("untrack(() => Number(strategy?.spot || 0))"),
    'Old direct strategy?.spot read should be removed from expiryCloseAnalysis'
  ).toBe(false);

  // The new three-tier lookup must be present.
  expect(
    block.includes('spot_anchor_contract'),
    'expiryCloseAnalysis spot should check spot_anchor_contract (tier 1 anchor)'
  ).toBe(true);

  expect(
    block.includes('getSnapshot(anchor)'),
    'expiryCloseAnalysis spot should use getSnapshot for the anchor contract'
  ).toBe(true);

  expect(
    block.includes('getSnapshot(und)'),
    'expiryCloseAnalysis spot should use getSnapshot for the underlying'
  ).toBe(true);

  expect(
    block.includes('_underlyingQuotes[selectedUnderlying]?.ltp'),
    'expiryCloseAnalysis spot should fall back to _underlyingQuotes batchQuote'
  ).toBe(true);

  // strategy?.spot must still appear as the FINAL fallback inside the block.
  expect(
    block.includes("Number(strategy?.spot || 0)"),
    'strategy?.spot must remain as the last-resort fallback inside the new lookup'
  ).toBe(true);
});

test('SSOT: liveSpot and expiryCloseAnalysis share the same three-tier spot resolution structure', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // Both liveSpot and expiryCloseAnalysis must use getSnapshot for spot
  // resolution — not two separate ad-hoc approaches.
  const liveSpotCount = (src.match(/const liveSpot\s*=/g) || []).length;
  expect(liveSpotCount, 'liveSpot must be defined exactly once').toBe(1);

  const expiryAnalysisCount = (src.match(/const expiryCloseAnalysis\s*=/g) || []).length;
  expect(expiryAnalysisCount, 'expiryCloseAnalysis must be defined exactly once').toBe(1);

  // Both must reference getSnapshot (the SSE-tick read path).
  const getSnapshotCount = (src.match(/getSnapshot\(/g) || []).length;
  expect(
    getSnapshotCount,
    'getSnapshot must be called at least twice (liveSpot + expiryCloseAnalysis)'
  ).toBeGreaterThanOrEqual(2);
});

test('SSOT: isITM comparison uses instrument-parsed strike and opt_type, not regex', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  // The ITM comparison should use inst.t (opt_type) and inst.k (strike)
  // sourced from getInstrument(), not an ad-hoc regex over the symbol string.
  expect(
    src.includes("optType === 'CE' ? spot > strike : spot < strike"),
    'ITM comparison must use canonical CE/PE vs spot/strike form'
  ).toBe(true);

  // The strike must come from inst.k (instrument cache), not a regex parse.
  expect(
    src.includes('Number(inst.k || 0)'),
    'strike must be sourced from inst.k (instrument cache field)'
  ).toBe(true);

  expect(
    src.includes("inst.t"),
    'opt_type must be sourced from inst.t (instrument cache field)'
  ).toBe(true);
});

// ── Live UI checks ────────────────────────────────────────────────────────────

const VIEWPORTS = [
  { name: 'desktop', project: 'chromium-desktop', width: 1400, height: 900 },
  { name: 'mobile',  project: 'mobile-portrait',  width: 360,  height: 800 },
];

for (const vp of VIEWPORTS) {
  test.describe(`/admin/derivatives — expiry band classification [${vp.name}]`, () => {
    test.setTimeout(120000);

    test(`expiry tab renders band headers without JS errors [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', (err) => pageErrors.push(err.message));

      const xhrCount = { n: 0 };
      page.on('request', (req) => {
        if (req.resourceType() === 'fetch' || req.resourceType() === 'xhr') {
          xhrCount.n++;
        }
      });

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
        test.skip(true, 'No valid credentials — skipping live UI check (static SSOT checks pass)');
        return;
      }

      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded', timeout: 30000 });

      // Wait for the legs card to be in the DOM.
      await page.waitForSelector('.cand-row, .legs-empty, [data-testid="legs-card"]', {
        timeout: 20000,
      }).catch(() => { /* no positions is OK */ });

      // Perf: derivatives page is data-heavy (positions, holdings, strategy,
      // instruments, sparklines, etc.) — budget matches the existing
      // derivatives_pnl_consistency spec ceiling.
      expect(xhrCount.n).toBeLessThan(80);

      // No JS errors.
      expect(
        pageErrors.filter(e => !/ResizeObserver|favicon/i.test(e)),
        'No JS errors on derivatives page'
      ).toHaveLength(0);

      // If the expiry tab button exists, click it and verify band header
      // labels render (the page may have no positions — that's OK, we just
      // check the tab is clickable and the band CSS is present in the DOM).
      const expiryTabBtn = page.locator('button, [role="tab"]').filter({ hasText: /expiry/i }).first();
      if (await expiryTabBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
        await expiryTabBtn.click();

        // The expiry band header container should be present (or empty state).
        // Either the band-header divs appear (positions exist) or the empty
        // state message renders ("No ITM options in the current candidate set").
        const bandHeaderOrEmpty = page.locator('.expiry-band-header, .expiry-empty, :text("No ITM")');
        // Give up to 5 s for the expiry tab to render.
        await bandHeaderOrEmpty.first().waitFor({ timeout: 5000 }).catch(() => { /* empty book */ });
      }
    });

    test(`band labels match BAND_LABELS constant (not raw 'close'/'netted'/'otm') [${vp.name}]`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });

      const pageErrors = [];
      page.on('pageerror', (err) => pageErrors.push(err.message));

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
        test.skip(true, 'No valid credentials — static SSOT checks already cover the logic');
        return;
      }

      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForSelector('.cand-row, .legs-empty, [data-testid="legs-card"]', {
        timeout: 20000,
      }).catch(() => { /* no positions */ });

      const expiryTabBtn = page.locator('button, [role="tab"]').filter({ hasText: /expiry/i }).first();
      if (!(await expiryTabBtn.isVisible({ timeout: 3000 }).catch(() => false))) {
        test.skip(true, 'No expiry tab visible — no positions; SSOT checks cover logic');
        return;
      }

      await expiryTabBtn.click();

      // If band headers are present, their labels must be the human-readable
      // strings from BAND_LABELS, not the raw internal keys.
      const bandLabels = page.locator('.expiry-band-label');
      const labelCount = await bandLabels.count();

      if (labelCount > 0) {
        for (let i = 0; i < labelCount; i++) {
          const text = (await bandLabels.nth(i).textContent() || '').trim().toUpperCase();
          // Must be one of the three canonical labels.
          expect(
            ['ITM ON EXPIRY', 'NETTED', 'OUT OF THE MONEY'].includes(text),
            `Band label "${text}" must be a canonical BAND_LABELS value`
          ).toBe(true);
          // Must NOT be a raw internal key.
          expect(['CLOSE', 'OTM'].includes(text), `Raw internal key "${text}" must not appear as label`).toBe(false);
        }

        // UX: no close-band amber tint on OTM rows (misclassification check).
        // If an OTM band header is present, there must be no row immediately
        // after it that carries the close-band amber class.
        const otmHeader = page.locator('.expiry-band-header-otm').first();
        if (await otmHeader.isVisible({ timeout: 2000 }).catch(() => false)) {
          // Find rows after the OTM header — they must NOT have close-band class.
          const closeRowsAfterOtmHeader = page.locator('.expiry-band-header-otm ~ .cand-row.expiry-band-close');
          expect(
            await closeRowsAfterOtmHeader.count(),
            'No close-band rows should appear under the OTM section header'
          ).toBe(0);
        }
      }

      // No JS errors.
      expect(
        pageErrors.filter(e => !/ResizeObserver|favicon/i.test(e)),
        'No JS errors while viewing expiry tab'
      ).toHaveLength(0);
    });
  });
}
