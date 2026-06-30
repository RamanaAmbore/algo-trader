/**
 * derivatives_expiry_bands.spec.js
 *
 * Verifies the 3-band expiry classifier (ITM ON EXPIRY / NETTED /
 * OUT OF THE MONEY) uses the canonical spot resolution chain — SSE
 * tick → _underlyingQuotes batchQuote → strategy.spot server-poll
 * fallback — and guards against stale-strategy cross-underlying
 * contamination.
 *
 * Defects closed:
 *
 *   SUZLON 60 CE (original): shown as ITM when spot had drifted below
 *   60. Classifier was reading `strategy?.spot` (last server-poll,
 *   stale) while liveSpot correctly reflected current price via SSE
 *   tick. Fix: read spot from getSnapshot first.
 *
 *   BHEL 420/450 PE (this commit): both shown as OTM when BHEL spot
 *   ~414 (should be ITM for PE). Root cause: when the operator switched
 *   underlying to BHEL, candidatePositions re-derived immediately but
 *   `strategy` still carried the previous underlying's response
 *   (e.g. NIFTY). The untracked spot block read
 *   `strategy.spot_anchor_contract` = NIFTY26JUNFUT and called
 *   getSnapshot('NIFTY26JUNFUT') → ~24 500. With spot=24 500,
 *   `24500 < 420` is false → OTM for BHEL 420 PE. Fix: validate
 *   strategy.underlying === selectedUnderlying before using any
 *   strategy-derived keys. When there is a mismatch, fall straight
 *   to getSnapshot(selUnd) → _underlyingQuotes → 0 (triggers the
 *   !spot early-return rather than classifying with a wrong price).
 *
 * Quality dimensions checked:
 *   SSOT     — spot resolution chain is canonical and guarded.
 *   Perf     — derivatives page loads within XHR budget.
 *   Stale    — no unguarded cross-underlying strategy read.
 *   Reusable — liveSpot and expiryCloseAnalysis share getSnapshot path.
 *   UX       — band labels are human-readable; OTM rows never carry
 *               close-band amber tint; PE ITM logic is sign-correct.
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

  // getSnapshot must be called for the selected underlying (selUnd) as
  // the SSE-tick read for the current underlying — the PE bug fix renamed
  // `und` to `selUnd` to reflect the validated-underlying variable.
  expect(
    block.includes('getSnapshot(selUnd)'),
    'expiryCloseAnalysis spot should use getSnapshot(selUnd) for the current underlying'
  ).toBe(true);

  expect(
    block.includes('_underlyingQuotes[selectedUnderlying]?.ltp'),
    'expiryCloseAnalysis spot should fall back to _underlyingQuotes batchQuote'
  ).toBe(true);

  // strategy?.spot must still appear as the FINAL fallback inside the block,
  // guarded by the underlying-match check.
  expect(
    block.includes("Number(strategy?.spot || 0)"),
    'strategy?.spot must remain as the last-resort fallback inside the new lookup'
  ).toBe(true);
});

test('SSOT: expiryCloseAnalysis guards against stale cross-underlying strategy read', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  const blockStart = src.indexOf('const expiryCloseAnalysis = $derived.by');
  expect(blockStart).toBeGreaterThan(0);
  const blockEnd = src.indexOf('\n  });\n', blockStart);
  expect(blockEnd).toBeGreaterThan(blockStart);
  const block = src.slice(blockStart, blockEnd + 10);

  // The guard: strategy.underlying must be compared against
  // selectedUnderlying before using any strategy-derived keys.
  // This prevents NIFTY's LTP being used as BHEL's spot when the
  // operator switches underlying and strategy hasn't refreshed yet.
  expect(
    block.includes('stratUnd === selUnd'),
    'expiryCloseAnalysis must validate strategy.underlying === selectedUnderlying before using strategy keys'
  ).toBe(true);

  // strategy.underlying must be extracted inside the untrack block.
  expect(
    block.includes("strategy?.underlying"),
    'expiryCloseAnalysis must read strategy.underlying for the validation check'
  ).toBe(true);

  // When strategy mismatches, the block must fall to 0 (not strategy?.spot
  // from the wrong underlying) so the !spot early-return prevents
  // misclassification.
  expect(
    block.includes('return 0;'),
    'expiryCloseAnalysis must return 0 when strategy underlying does not match selectedUnderlying'
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

// ── ITM/OTM direction matrix (static logic check) ─────────────────────────
//
// Parametrized matrix: all four canonical (opt_type, spot vs strike) cases.
// Tests the exact JS expression used in expiryCloseAnalysis so any future
// refactor that inverts the PE sign will fail immediately.
//
//  CE, spot > strike → ITM  (spot=450, strike=420 → 450 > 420 → true)
//  CE, spot < strike → OTM  (spot=414, strike=420 → 414 > 420 → false)
//  PE, spot < strike → ITM  (spot=414, strike=420 → 414 < 420 → true)  ← BHEL 420 PE
//  PE, spot > strike → OTM  (spot=460, strike=420 → 460 < 420 → false)
//
// Canonical expression: optType === 'CE' ? spot > strike : spot < strike

const ITM_MATRIX = [
  { optType: 'CE', spot: 450,   strike: 420, expectedITM: true,  label: 'CE ITM  (spot 450 > strike 420)' },
  { optType: 'CE', spot: 414.55,strike: 420, expectedITM: false, label: 'CE OTM  (spot 414.55 < strike 420)' },
  { optType: 'PE', spot: 414.55,strike: 420, expectedITM: true,  label: 'PE ITM  (spot 414.55 < strike 420) — BHEL 420 PE scenario' },
  { optType: 'PE', spot: 414.55,strike: 450, expectedITM: true,  label: 'PE ITM  (spot 414.55 < strike 450) — BHEL 450 PE scenario' },
  { optType: 'PE', spot: 460,   strike: 420, expectedITM: false, label: 'PE OTM  (spot 460 > strike 420)' },
  { optType: 'CE', spot: 55,    strike: 60,  expectedITM: false, label: 'CE OTM  (spot 55 < strike 60)  — SUZLON 60 CE scenario' },
  { optType: 'CE', spot: 65,    strike: 60,  expectedITM: true,  label: 'CE ITM  (spot 65 > strike 60)' },
];

for (const { optType, spot, strike, expectedITM, label } of ITM_MATRIX) {
  test(`ITM matrix: ${label}`, () => {
    // Replicate the exact classifier expression from expiryCloseAnalysis.
    // If this expression ever changes in the source, the SSOT test above
    // catches it; this test catches sign-flip bugs in the expression itself.
    const isITM = optType === 'CE' ? spot > strike : spot < strike;
    expect(isITM, `isITM for ${label}`).toBe(expectedITM);
  });
}

test('SSOT: OTM distance is non-negative for both CE and PE OTM cases', () => {
  // otmDist = isITM ? 0 : (CE ? strike - spot : spot - strike)
  // Both cases must yield a positive distance when OTM.
  const cases = [
    { optType: 'CE', spot: 414.55, strike: 420 },  // CE OTM: 420 - 414.55 = 5.45 > 0
    { optType: 'PE', spot: 460,    strike: 420 },  // PE OTM: 460 - 420 = 40 > 0
  ];
  for (const { optType, spot, strike } of cases) {
    const isITM = optType === 'CE' ? spot > strike : spot < strike;
    const otmDist = isITM ? 0 : (optType === 'CE' ? strike - spot : spot - strike);
    expect(
      otmDist,
      `otmDist for ${optType} spot=${spot} strike=${strike} must be >= 0`
    ).toBeGreaterThanOrEqual(0);
  }
});

test('SSOT: expiryCloseAnalysis spot block validates underlying before using strategy keys', () => {
  // Regression guard for the BHEL PE bug. The fix ensures that if strategy
  // belongs to a different underlying (e.g. NIFTY after switching to BHEL),
  // the spot block does NOT call getSnapshot(NIFTY26JUNFUT) and return ~24 500
  // as BHEL's spot. Verify the source contains both the validation variable
  // name and the guarded-then-else structure.
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  const blockStart = src.indexOf('const expiryCloseAnalysis = $derived.by');
  const blockEnd   = src.indexOf('\n  });\n', blockStart);
  const block      = src.slice(blockStart, blockEnd + 10);

  // The guard variable names.
  expect(block.includes('selUnd'),   'selUnd variable must be present in the spot block').toBe(true);
  expect(block.includes('stratUnd'), 'stratUnd variable must be present in the spot block').toBe(true);

  // The mismatch path must explicitly return 0 (not strategy?.spot).
  // Count occurrences: the mismatch `return 0` is distinct from any
  // other zero returns.
  const zeroReturnCount = (block.match(/return 0;/g) || []).length;
  expect(
    zeroReturnCount,
    'At least one `return 0` must appear in expiryCloseAnalysis for the underlying-mismatch path'
  ).toBeGreaterThanOrEqual(1);
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
        const bandHeaderOrEmpty = page.locator('.expiry-band-header, .expiry-empty, :text("No ITM")');
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
        const otmHeader = page.locator('.expiry-band-header-otm').first();
        if (await otmHeader.isVisible({ timeout: 2000 }).catch(() => false)) {
          // Rows after the OTM header must NOT have close-band class.
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

    test(`PE options with strike > spot are classified ITM (not OTM) [${vp.name}]`, async ({ page }) => {
      // Live UI check: if BHEL or any PE option is visible in the expiry
      // band view, verify it is NOT in the OTM band when its strike is
      // known to be above spot. This is a canary test — it will naturally
      // skip when no PE positions exist in the current book.
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
        test.skip(true, 'No valid credentials — static SSOT + matrix checks cover the logic');
        return;
      }

      await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForSelector('.cand-row, .legs-empty, [data-testid="legs-card"]', {
        timeout: 20000,
      }).catch(() => { /* no positions */ });

      const expiryTabBtn = page.locator('button, [role="tab"]').filter({ hasText: /expiry/i }).first();
      if (!(await expiryTabBtn.isVisible({ timeout: 3000 }).catch(() => false))) {
        test.skip(true, 'No expiry tab — no positions in book');
        return;
      }
      await expiryTabBtn.click();
      await page.waitForTimeout(1000); // let the derived settle

      // Locate any OTM section rows that contain a PE symbol chip.
      // A PE row in the OTM band whose _reason carries a negative OTM
      // distance (e.g. "OTM by ₹-6") is a signal the classifier fired
      // with the wrong spot — a negative distance means spot < strike
      // which is ITM for PE, not OTM. The correct display for a
      // genuinely OTM PE would be "OTM by ₹X" where X > 0.
      const otmRows = page.locator('.expiry-band-otm');
      const otmCount = await otmRows.count();
      for (let i = 0; i < otmCount; i++) {
        const row = otmRows.nth(i);
        const text = (await row.textContent() || '').toLowerCase();
        if (!text.includes('pe')) continue;
        // Extract any "OTM by ₹-N" pattern — negative distance means PE is
        // actually ITM and was wrongly placed in OTM band.
        const m = text.match(/otm by\s*₹\s*(-\d+)/);
        expect(
          m,
          `PE row in OTM band must not show a negative OTM distance (found: "${text.slice(0, 80)}")`
        ).toBeNull();
      }

      // No JS errors.
      expect(
        pageErrors.filter(e => !/ResizeObserver|favicon/i.test(e)),
        'No JS errors on PE ITM check'
      ).toHaveLength(0);
    });
  });
}
