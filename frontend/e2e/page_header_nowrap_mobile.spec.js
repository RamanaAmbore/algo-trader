/**
 * page_header_nowrap_mobile.spec.js
 *
 * 2026-10-23 — `.page-header` (every algo page, (algo)/+layout.svelte) was
 * `flex-wrap: wrap`. The header is `position: fixed` while `.algo-content`'s
 * `padding-top` only reserves ONE row's height, so a wrapped second row
 * visually covered the first card underneath it — not just untidy.
 * Separately, `.page-header-actions` (the button group itself —
 * Refresh/Order/Chart/Log) had its own ≤640px override that let it wrap/
 * shrink away instead of staying pinned, the opposite of the project's
 * card-button-group invariant ("title/label AND its button-group always
 * visible; whatever sits between them scrolls horizontally when it
 * overflows — never wraps, never gets clipped").
 *
 * Fix: `.page-header` is now `nowrap`; `.algo-title-group` and
 * `.page-header-actions` are both pinned (flex-shrink: 0) on every
 * viewport; `AlgoTimestamp.svelte`'s `.ats-root` (the one child designed
 * to give) got `flex: 1 1 0` so it actually shrinks + internally scrolls
 * (via its existing `.ats-scroll`) instead of the whole row wrapping.
 *
 * This spec loops over a representative route from each nav group
 * (Monitor / Analyze / Explore / Config) at a 360px mobile viewport and
 * asserts, per the advisor's review of this fix:
 *   - `.page-header`'s own box height stays within a single-row bound
 *     (a wrapped 2-row header would be roughly double — measured ~26-30px
 *     for a real single row across every route below; the threshold is
 *     set well clear of that at 40px). A per-child `top`-offset comparison
 *     was tried first and rejected: `.page-header` uses `align-items:
 *     center`, so children of different intrinsic heights (an h1 line vs.
 *     an icon button) legitimately sit at different `top` values WITHIN
 *     one row — that is not evidence of wrapping;
 *   - `.page-header-actions`' right edge never runs past the viewport
 *     (proof pinning doesn't silently push the button group off-screen —
 *     the exact failure mode the removed mobile relaxation was guarding
 *     against, which no longer applies now that the page that motivated
 *     it, /automation, no longer injects the extra pills that comment
 *     described);
 *   - the title text itself is still visible (didn't wrap inside its own
 *     chip once pinned);
 *   - the document itself never horizontally overflows the viewport.
 *
 * Run:
 *   cd frontend && npx playwright test \
 *     e2e/page_header_nowrap_mobile.spec.js --project=mobile-portrait
 */
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const ROUTES = [
  '/orders',             // monitor
  '/pulse',              // monitor
  '/dashboard',          // monitor
  '/admin/derivatives',  // monitor (dynamic SIMULATOR pill possible)
  '/charts',             // monitor
  '/automation',         // monitor
  '/strategies',         // monitor
  '/admin/execution',    // explore
  '/admin/settings',     // config
  '/admin/history',      // config
  '/admin',              // config — "+ Create User" is an unpinned
                         // direct `.page-header` child button (see
                         // the `.page-header > button` CSS rule)
  '/admin/tokens',       // config — same "+ New token" button pattern
];

test.describe('Page header — nowrap + pinned button group at 360px', () => {
  test.use({ viewport: { width: 360, height: 800 } });

  for (const path of ROUTES) {
    test(`${path}: header stays one row; button group stays visible + on-screen`, async ({ page }) => {
      await loginAsAdmin(page);
      await page.goto(path, { waitUntil: 'domcontentloaded', timeout: 30_000 });

      const header = page.locator('.page-header').first();
      await expect(header).toBeVisible({ timeout: 15_000 });

      const titleGroup = header.locator('.algo-title-group').first();
      const actions = header.locator('.page-header-actions').first();
      await expect(titleGroup).toBeVisible();
      await expect(actions).toBeVisible();

      // `.page-header`'s own box stays within a single-row height — a
      // real single row across every representative route measures
      // ~26-30px; a wrapped 2-row header would be roughly double that.
      const headerBox = await header.boundingBox();
      expect(headerBox).not.toBeNull();
      expect(headerBox.height).toBeLessThanOrEqual(40);

      // The button group's right edge never runs past the 360px viewport.
      const actionsBox = await actions.boundingBox();
      expect(actionsBox).not.toBeNull();
      expect(actionsBox.x + actionsBox.width).toBeLessThanOrEqual(361);

      // The title text is still non-empty/visible (not squeezed to zero
      // width, not wrapped internally into something unreadable).
      const titleText = (await titleGroup.textContent())?.trim() ?? '';
      expect(titleText.length).toBeGreaterThan(0);

      // The page itself must not horizontally overflow the viewport.
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);
    });
  }
});
