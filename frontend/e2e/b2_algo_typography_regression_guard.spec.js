// B2 algo-page typography regression guard (2026-09)
//
// B2 (commit d9de4d04) scoped its typography floor + Inter-font drop to
// (public) + investor routes only, verified via a source-level diff-proof
// check (public_typography_floor.spec.js) that app.css's bare `body {}`
// rule text (font-size: 0.8125rem) is byte-identical to before. That
// static check pins the CSS source, but the task's own "critical
// regression guard" calls for confirming /dashboard and /pulse render
// with ZERO visual/font-size change — i.e. a LIVE computed-style check,
// not just a source-text match (a scoping bug elsewhere, e.g. a stray
// :global() leaking a (public) rule into the algo layout, would pass the
// source diff-proof but fail here).
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — reads the real rendered computed style of document.body
//                on /dashboard and /pulse, not the CSS source text.
//   2. Perf    — reuses the cached-token login fast path (fixtures/auth.js),
//                same pattern as algo_consistency.spec.js.
//   3. Stale   — n/a; guards against future (public)-scoped rules leaking.
//   4. Reuse   — shares loginAsAdmin() rather than a bespoke login flow.
//   5. UX      — pins BOTH font-size (13px) and font-family (the algo
//                `ui-sans-serif, system-ui, sans-serif` stack, never
//                'Inter') so a future (public)-only font-family change
//                can't silently bleed into the trading desk UI either.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const ALGO_ROUTES = ['/dashboard', '/pulse'];
const EXPECTED_FONT_SIZE_PX = 13; // 0.8125rem at the default 16px root
const FORBIDDEN_FONT = /inter/i;

for (const route of ALGO_ROUTES) {
  test(`${route} — body font-size stays 13px, font-family stays the algo stack (not Inter)`, async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(route, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);

    const style = await page.evaluate(() => {
      const cs = getComputedStyle(document.body);
      return { fontSize: cs.fontSize, fontFamily: cs.fontFamily };
    });

    expect(parseFloat(style.fontSize)).toBe(EXPECTED_FONT_SIZE_PX);
    expect(style.fontFamily, `body font-family on ${route} must not resolve to Inter`)
      .not.toMatch(FORBIDDEN_FONT);
  });
}
