// Public /performance — B4 surface-card layering (2026-09)
//
// Adds a near-white "surface" card layer (.perf-root) atop the cream page
// background, mirroring the Financial Times' near-white article/data-
// module treatment. Scoped via `:global(.card-theme-cream) .perf-root
// :not(.perf-dark)` in PerformancePage.svelte, backed by two new CSS
// custom properties (--card-surface-bg / --card-surface-border) added to
// the existing .card-theme-cream token set in app.css — same face color
// already used by .pub-card / .pub-form-panel-body (#fffdf8 / #ddd8ce),
// reused rather than reinvented.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — reads the real computed background/border of the
//                rendered .perf-root element and the real resolved value
//                of the --card-surface-bg/-border custom properties on
//                .card-theme-cream, not hardcoded assumptions.
//   2. Perf    — reuses the existing unauthenticated /performance route,
//                no extra fixtures/mocking needed.
//   3. Stale   — n/a (new surface, additive-only change).
//   4. Reuse   — asserts the surface reuses the SAME token values as
//                .pub-card (#fffdf8 / #ddd8ce), not a new one-off color.
//   5. UX      — desktop card renders with visible border/background;
//                mobile (<600px) keeps the card but with reduced padding
//                (mobile-first: cells must still fit a phone viewport);
//                the surface never contaminates the page background
//                color itself (--card-bg, the page/card-shell token,
//                stays distinct from --card-surface-bg).

import { test, expect } from '@playwright/test';

test.describe('Public /performance — B4 surface-card layer', () => {
  test('desktop — .perf-root renders the near-white surface card, reusing .pub-card tokens', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/performance');

    const root = page.locator('.perf-root');
    await root.waitFor({ state: 'visible', timeout: 10_000 });

    const style = await root.evaluate((el) => {
      const cs = getComputedStyle(el);
      const parentCs = getComputedStyle(el.closest('.card-theme-cream'));
      return {
        background: cs.backgroundColor,
        borderColor: cs.borderColor,
        borderWidth: cs.borderTopWidth,
        borderRadius: cs.borderTopLeftRadius,
        tokenBg: parentCs.getPropertyValue('--card-surface-bg').trim(),
        tokenBorder: parentCs.getPropertyValue('--card-surface-border').trim(),
        pageBg: parentCs.getPropertyValue('--card-bg').trim(),
      };
    });

    // Same face color already used by .pub-card / .pub-form-panel-body.
    expect(style.tokenBg).toBe('#fffdf8');
    expect(style.tokenBorder).toBe('#ddd8ce');
    expect(style.background).toBe('rgb(255, 253, 248)'); // #fffdf8
    expect(style.borderColor).toBe('rgb(221, 216, 206)'); // #ddd8ce
    expect(style.borderWidth).toBe('1px');
    expect(parseFloat(style.borderRadius)).toBeGreaterThan(0);

    // The surface token must stay distinct from the page/card-shell
    // background token — confirms this is a genuine second, lighter
    // layer, not a rename of the existing page bg.
    expect(style.tokenBg).not.toBe(style.pageBg);
  });

  test('mobile (<600px) — surface card persists with reduced padding, not removed', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/performance');

    const root = page.locator('.perf-root');
    await root.waitFor({ state: 'visible', timeout: 10_000 });

    const style = await root.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { background: cs.backgroundColor, paddingLeft: parseFloat(cs.paddingLeft) };
    });

    expect(style.background).toBe('rgb(255, 253, 248)');
    // Desktop left padding is 1rem (16px); mobile must be strictly
    // smaller so grid columns keep their usual room on a phone viewport.
    expect(style.paddingLeft).toBeLessThan(16);
    expect(style.paddingLeft).toBeGreaterThan(0);
  });

  test('no horizontal overflow at 390px width (mobile cells fit the viewport)', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/performance');
    await page.locator('.perf-root').waitFor({ state: 'visible', timeout: 10_000 });

    const overflow = await page.evaluate(() => {
      const root = document.querySelector('.perf-root');
      return root.scrollWidth - root.clientWidth;
    });
    expect(overflow).toBeLessThanOrEqual(1); // allow 1px rounding
  });
});
