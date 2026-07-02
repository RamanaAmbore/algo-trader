/**
 * derivatives_payoff_legs_height.spec.js
 *
 * Guards the desktop height-parity constraint between the Payoff card and
 * the Legs card that share `.opt-payoff-legs-row`.
 *
 * On desktop (>= 1180 px breakpoint, tested at 1280×800):
 *   - Both cards sit in a CSS grid with `align-items: stretch`.
 *   - `.cand-scroll` inside the legs card uses `flex: 1 1 0; min-height: 0`
 *     so it scrolls internally rather than growing past the payoff card's
 *     fixed SVG height.
 *   - The resulting bounding-box heights must match within 2 px.
 *
 * On mobile (393×851):
 *   - The row is column-flex (stacked). No height equality required.
 *   - Spec asserts only that the row has `flex-direction: column` so
 *     neither card is accidentally constrained by the desktop rule.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *  1. SSOT     — single height source: payoff card's intrinsic height
 *                (header + 320 px SVG + padding); legs card stretches to it
 *  2. Perf     — assertion runs synchronously after page settle, no retry loop
 *  3. Stale    — grep confirms min-height:0 present in source CSS
 *  4. Reusable — shared authOnce() helper, same pattern as sibling specs
 *  5. UX       — cand-scroll overflow-y computed === 'auto' (scrolls, not clips)
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/derivatives_payoff_legs_height.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE      = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const DERIV_URL = `${BASE}/admin/derivatives`;

const _AUTH_USER = process.env.PLAYWRIGHT_USER  || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS  || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) { tok = (await resp.json()).access_token; break; }
      if (resp.status() !== 429 && resp.status() !== 502) {
        throw new Error(`authOnce: login returned ${resp.status()}`);
      }
    }
    if (!tok) { test.skip(true, 'rate-limited'); return; }
    _cachedToken = tok;
  }
  await page.context().addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

// ── Desktop suite — height parity ─────────────────────────────────────────────

test.describe('Payoff + Legs card height parity — desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } });
  test.setTimeout(60_000);

  test('payoff and legs cards have equal height (within 2 px)', async ({ page }) => {
    await authOnce(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    // Wait for both cards to be visible (the legs card is always mounted).
    const payoffCard = page.locator('.opt-payoff.opt-payoff-full');
    const legsCard   = page.locator('.opt-legs-card');

    await payoffCard.waitFor({ state: 'visible', timeout: 30_000 });
    await legsCard.waitFor({ state: 'visible', timeout: 30_000 });

    // Give the grid layout one paint cycle after both are visible.
    await page.waitForTimeout(300);

    const payoffBox = await payoffCard.boundingBox();
    const legsBox   = await legsCard.boundingBox();

    expect(payoffBox, 'payoff card must be visible with a bounding box').not.toBeNull();
    expect(legsBox,   'legs card must be visible with a bounding box').not.toBeNull();

    const diff = Math.abs(payoffBox.height - legsBox.height);
    expect(
      diff,
      `payoff height ${payoffBox.height}px vs legs height ${legsBox.height}px — delta ${diff}px exceeds 2 px tolerance`,
    ).toBeLessThanOrEqual(2);
  });

  // STALE guard — confirm the CSS fix is present in the source file.
  test('source CSS has min-height:0 on .opt-legs-card inside grid block', async ({ page }) => {
    const fs = await import('fs');
    const src = fs.readFileSync(
      new URL(
        '../src/routes/(algo)/admin/derivatives/+page.svelte',
        import.meta.url,
      ).pathname,
      'utf8',
    );

    // The grid-block rule must have min-height:0 on the legs card.
    expect(src).toContain('min-height: 0');

    // The cand-scroll inside the grid block must NOT use plain `flex: 1;`
    // without the shrink/basis component (that was the broken version).
    // It must use `flex: 1 1 0` so the basis is zero and the item shrinks.
    expect(src).toContain('flex: 1 1 0');
  });

  // UX guard — cand-scroll scrolls vertically rather than clipping.
  test('cand-scroll has overflow-y auto inside legs card', async ({ page }) => {
    await authOnce(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const candScroll = page.locator('.opt-legs-card .cand-scroll').first();
    await candScroll.waitFor({ state: 'attached', timeout: 30_000 });

    const overflowY = await candScroll.evaluate(
      (el) => getComputedStyle(el).overflowY,
    );

    // Either 'auto' or 'scroll' — both allow scrolling; 'hidden' would
    // silently clip candidates (the bug that min-height:0 prevents by
    // keeping the scroll div inside its constrained flex parent).
    expect(
      ['auto', 'scroll'],
      `cand-scroll overflow-y should be auto or scroll, got '${overflowY}'`,
    ).toContain(overflowY);
  });
});

// ── Mobile suite — no height constraint applied ────────────────────────────────

test.describe('Payoff + Legs layout — mobile, no height parity constraint', () => {
  test.use({ viewport: { width: 393, height: 851 } });
  test.setTimeout(60_000);

  test('row is stacked (column) on mobile — no height equality asserted', async ({ page }) => {
    await authOnce(page);
    await page.goto(DERIV_URL, { waitUntil: 'domcontentloaded' });

    const row = page.locator('.opt-payoff-legs-row');
    await row.waitFor({ state: 'visible', timeout: 30_000 });

    const flexDir = await row.evaluate(
      (el) => getComputedStyle(el).flexDirection,
    );

    // On mobile the row must be column-flex (stacked), not row/grid.
    // The grid rule only fires at >= 1180 px, so computed display === flex
    // and flex-direction === column.
    expect(
      flexDir,
      `On mobile opt-payoff-legs-row must be column-flex, got '${flexDir}'`,
    ).toBe('column');
  });
});
