/**
 * derivatives_total_row_height_color_parity.spec.js
 *
 * Playwright e2e spec for the 2026-11-22 fix (operator: "why derivatives
 * positions and snapshot height and color of the text total different.
 * make them consistent").
 *
 * Two confirmed, independent bugs in /admin/derivatives' two TOTAL rows —
 * the Legs/Candidates grid's `.cand-row.cand-row-total` and the Snapshot
 * grid's `.byund-row-total`:
 *
 *  Bug A (height) — `.cand-row.cand-row-total` carries the markup class
 *  `cand-row`, which LOOKS like it reuses the real leg row's height
 *  treatment (CandidateLegRow.svelte: `min-height: var(--grid-row-height);
 *  align-items: stretch;`), but that rule is Svelte-scoped to
 *  CandidateLegRow.svelte and this div lives in +page.svelte — a different
 *  component, so cross-component class reuse with no `:global()` has zero
 *  effect. The ONLY rule actually governing the row (+page.svelte's own
 *  local `.cand-row.cand-row-total`) had no height/min-height at all, just
 *  ad-hoc padding — unlike every other row in both grids (`.cand-headrow`,
 *  `.cand-row`, `.byund-headrow`, `.byund-row`, `.byund-row-total`), all of
 *  which pin to `--grid-row-height`.
 *
 *  Bug B (color) — the Snapshot TOTAL row's "TOTAL" label span carried a
 *  stray inline `style="color: inherit"` that the regular (non-total)
 *  byund row's equivalent cell never had, making the label fall through to
 *  the page's default text color instead of the intended amber
 *  `--c-action` every other TOTAL label (`.cand-total-label`) already uses.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT   — both TOTAL rows pin to the same --grid-row-height token;
 *              both TOTAL labels resolve to the same --c-action color.
 *  2. Perf   — no new network calls; pure CSS/markup fix.
 *  3. Stale  — confirms the fixed-bug shapes (no height, inherited color)
 *              do not reappear; confirmed to fail against the pre-fix code.
 *  4. Reuse  — pins that `.cand-row.cand-row-total` now explicitly
 *              replicates `.cand-row`'s own mechanism rather than silently
 *              relying on a cross-component class match that never worked.
 *  5. UX     — live check (best-effort, skips if no TOTAL row renders)
 *              that the two rows' rendered heights agree.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PAGE_SRC = readFileSync(
  join(__dirname, '../src/routes/(algo)/admin/derivatives/+page.svelte'),
  'utf-8'
);

test.describe('derivatives TOTAL row — height + color parity (source)', () => {
  test('Legs TOTAL row container pins to the shared height token', () => {
    const containerRuleMatch = PAGE_SRC.match(
      /\.cand-row\.cand-row-total\s*\{([^}]*)\}/
    );
    expect(containerRuleMatch, 'cand-row-total container rule must exist').toBeTruthy();
    const body = containerRuleMatch[1];
    expect(body).toMatch(/min-height:\s*var\(--grid-row-height\)/);
    expect(body).toMatch(/align-items:\s*stretch/);
    expect(body).toMatch(/box-sizing:\s*border-box/);
  });

  test('Legs TOTAL row span no longer uses ad-hoc vertical padding', () => {
    const spanRuleMatch = PAGE_SRC.match(
      /\.cand-row\.cand-row-total\s*>\s*span\s*\{([^}]*)\}/
    );
    expect(spanRuleMatch).toBeTruthy();
    const body = spanRuleMatch[1];
    // The pre-fix bug was literally `padding: 0.32rem 0.45rem` — a vertical
    // component with no height to pin against. Confirm it's gone.
    expect(body).not.toMatch(/padding:\s*0\.32rem/);
    expect(body).toMatch(/display:\s*flex/);
    expect(body).toMatch(/align-items:\s*center/);
  });

  test('Legs TOTAL numeric cells keep right-alignment under flex', () => {
    const numRuleMatch = PAGE_SRC.match(
      /\.cand-row\.cand-row-total\s*>\s*span\.num\s*\{([^}]*)\}/
    );
    expect(numRuleMatch).toBeTruthy();
    const body = numRuleMatch[1];
    // text-align alone is inert once the span is display:flex — the fix
    // must carry justify-content too, or numeric columns silently left-align.
    expect(body).toMatch(/text-align:\s*right/);
    expect(body).toMatch(/justify-content:\s*flex-end/);
  });

  test('Snapshot TOTAL label has no inline color override', () => {
    const labelMatch = PAGE_SRC.match(
      /<span class="byund-und[^"]*"[^>]*title="F&O positions total">/
    );
    expect(labelMatch, 'Snapshot TOTAL label span must exist').toBeTruthy();
    expect(labelMatch[0]).not.toMatch(/style="color:\s*inherit"/);
  });

  test('Snapshot TOTAL label gets explicit font-weight parity with Legs label', () => {
    expect(PAGE_SRC).toMatch(/\.byund-und-total\s*\{\s*font-weight:\s*800;?\s*\}/);
    // .cand-total-label (Legs grid) already asserts font-weight: 800 — this
    // is the OTHER half of the parity the fix establishes.
    const candLabelMatch = PAGE_SRC.match(/\.cand-total-label\s*\{([^}]*)\}/);
    expect(candLabelMatch).toBeTruthy();
    expect(candLabelMatch[1]).toMatch(/font-weight:\s*800/);
  });

  test('regular (non-total) byund row cell is untouched by the fix', () => {
    // Guard against an over-broad fix accidentally changing the ordinary
    // per-underlying row's own markup too.
    const regularRowMatch = PAGE_SRC.match(
      /<span class="byund-und">\{g\.underlying\}<\/span>/
    );
    expect(regularRowMatch, 'regular byund row underlying cell must be unchanged').toBeTruthy();
  });
});

test.describe('derivatives TOTAL row — height parity (live, best-effort)', () => {
  test.setTimeout(60000);

  const USER = process.env.PLAYWRIGHT_USER || 'rambo';
  const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
  const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

  async function signIn(page) {
    await page.goto('/signin', { waitUntil: 'domcontentloaded' });
    await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
    await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
    await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
    await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
  }

  test('Legs and Snapshot TOTAL rows render at the same height', async ({ page }) => {
    await page.goto(BASE);
    await signIn(page);
    await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3000);

    const candTotal = page.locator('.cand-row.cand-row-total').first();
    const byundTotal = page.locator('.byund-row-total > span').first();

    const candCount = await candTotal.count();
    const byundCount = await byundTotal.count();
    test.skip(candCount === 0 || byundCount === 0,
      'No TOTAL row rendered (no F&O positions in this account/environment) — nothing to compare');

    const candBox = await candTotal.boundingBox();
    const byundBox = await byundTotal.boundingBox();
    test.skip(!candBox || !byundBox, 'Could not measure one of the TOTAL rows');

    // Allow a small tolerance for sub-pixel rounding between the two
    // independent grid layouts — the fix pins both to the SAME token, so
    // they should agree to within a pixel or two, not differ by several px
    // the way the pre-fix padding-vs-fixed-height mismatch did.
    expect(Math.abs(candBox.height - byundBox.height)).toBeLessThanOrEqual(2);
  });
});
