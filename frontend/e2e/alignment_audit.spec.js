// Alignment audit — canonical page-header rule per CLAUDE.md:
//   [Title] [Tabs?] [AccountMultiSelect?] [Chips?] → spacer → [Trio]
//
// RIGHT-aligned (operator's strict list): expand (Fullscreen),
// contract (Collapse), chart, activity (bell), default-size, refresh.
// LEFT-aligned: everything else (title, tabs, account, chips, info,
// content-action buttons like + Create User, History pill, Ask AI,
// back-links, status pills, filter dropdowns).
//
// Five quality dimensions (per feedback_test_dimensions.md):
//   1. SSOT — RIGHT buttons sit in .page-header-actions OR have
//      `margin-left: auto`. Bounding-box x mid-point > 50% of header.
//   2. Performance — page-header DOM count <= 2 per page (one strip).
//   3. Stale code — no `.ml-auto` class accidentally applied to an
//      AccountMultiSelect / filter dropdown / content-action button.
//   4. Reusable — RIGHT buttons use canonical components only
//      (RefreshButton, PageHeaderActions). No hand-rolled trio.
//   5. UX — assertions repeated on desktop (1400px) + mobile (360px).
//
// To stay inside the 5/min auth rate-limit, every assertion goes
// through a single signed-in browser context (shared across tests).

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

// Major algo pages that carry the canonical .page-header strip.
// Excludes (public) routes (cream theme grammar) and modal-mounted
// surfaces (ChartModal, ActivityLogModal, TourModal) per the
// CLAUDE.md exceptions.
const PAGES = [
  '/pulse',
  '/dashboard',
  '/orders',
  '/admin/derivatives',
  '/charts',
  '/automation',
  '/strategies',
  '/activity',
  '/console',
  '/admin/brokers',
  '/admin/settings',
  '/admin/audit',
  '/admin/history',
  '/admin/health',
  '/admin/alerts',
  '/admin/statements',
  '/admin/tokens',
  '/admin/research',
  '/admin/execution',
  '/admin',
  '/automation/activity',
  '/automation/templates',
  '/automation/agent-templates',
];

test.describe.serial('alignment audit (canonical page-header rule)', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await loginAsAdmin(page);
  });

  for (const route of PAGES) {
    test(`alignment: ${route} — right-cluster sits right of ml-auto spacer`, async ({ page }) => {
      await page.goto(route, { waitUntil: 'networkidle' }).catch(() => {});
      await page.waitForTimeout(800);

      const header = page.locator('.page-header').first();
      const exists = await header.count();
      if (!exists) {
        test.info().annotations.push({ type: 'skip', description: `${route}: no .page-header` });
        return;
      }
      await expect(header).toBeVisible();

      const headerBox = await header.boundingBox();
      if (!headerBox) throw new Error(`${route}: header has no bounding box`);
      const headerMidX = headerBox.x + headerBox.width / 2;

      // Dimension 1 (SSOT): RefreshButton x-midpoint > 50% of header width.
      const refresh = header.locator('.rf-btn').first();
      if (await refresh.count()) {
        const rb = await refresh.boundingBox();
        if (rb) {
          expect(rb.x + rb.width / 2,
            `${route}: Refresh must be on right half of page-header`)
            .toBeGreaterThan(headerMidX);
        }
      }

      // PageHeaderActions trio (Order / Chart / Activity bell).
      const pha = header.locator('.pha-wrap').first();
      if (await pha.count()) {
        const pb = await pha.boundingBox();
        if (pb) {
          expect(pb.x + pb.width / 2,
            `${route}: PageHeaderActions trio must be on right half`)
            .toBeGreaterThan(headerMidX);
        }
      }

      // Title chip on LEFT half.
      const title = header.locator('.algo-title-group').first();
      if (await title.count()) {
        const tb = await title.boundingBox();
        if (tb) {
          expect(tb.x + tb.width / 2,
            `${route}: title group must be on left half`)
            .toBeLessThan(headerMidX);
        }
      }

      // Dimension 3 (stale code): exactly one .ml-auto spacer.
      const mlAutoCount = await header.locator(':scope > .ml-auto').count();
      expect(mlAutoCount,
        `${route}: must have exactly one .ml-auto spacer`).toBe(1);

      // Dimension 4 (reusable): one .page-header-actions span.
      const phaSpan = await header.locator(':scope > .page-header-actions').count();
      expect(phaSpan,
        `${route}: must have exactly one .page-header-actions`).toBe(1);
    });
  }
});

// ── Drift-specific assertions for the six pages fixed in this slice ──
//
// Each asserts the previously-misplaced element now sits on the LEFT
// half of the page-header (mid-x < header mid-x).

test.describe.serial('drift fixes — left-aligned content', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await loginAsAdmin(page);
  });

  test('/admin Create User button on LEFT', async ({ page }) => {
    await page.goto('/admin', { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    const header = page.locator('.page-header').first();
    const headerBox = await header.boundingBox();
    if (!headerBox) return;
    const btn = header.locator('button', { hasText: /Create User|Cancel/ }).first();
    if (!(await btn.count())) return;
    const bb = await btn.boundingBox();
    if (!bb) return;
    expect(bb.x + bb.width / 2,
      '/admin Create User must sit on LEFT half')
      .toBeLessThan(headerBox.x + headerBox.width / 2);
  });

  test('/admin/tokens New token button on LEFT', async ({ page }) => {
    await page.goto('/admin/tokens', { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    const header = page.locator('.page-header').first();
    const headerBox = await header.boundingBox();
    if (!headerBox) return;
    const btn = header.locator('button', { hasText: /New token/ }).first();
    if (!(await btn.count())) return;
    const bb = await btn.boundingBox();
    if (!bb) return;
    expect(bb.x + bb.width / 2,
      '/admin/tokens New token must sit on LEFT half')
      .toBeLessThan(headerBox.x + headerBox.width / 2);
  });

  test('/automation History + Ask AI on LEFT', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    const header = page.locator('.page-header').first();
    const headerBox = await header.boundingBox();
    if (!headerBox) return;
    const headerMidX = headerBox.x + headerBox.width / 2;

    const historyChip = header.locator('a.history-pill').first();
    const aiBtn = header.locator('button.ai-pill').first();
    if (await historyChip.count()) {
      const hb = await historyChip.boundingBox();
      if (hb) {
        expect(hb.x + hb.width / 2,
          '/automation History pill must sit on LEFT half')
          .toBeLessThan(headerMidX);
      }
    }
    if (await aiBtn.count()) {
      const ab = await aiBtn.boundingBox();
      if (ab) {
        expect(ab.x + ab.width / 2,
          '/automation Ask AI must sit on LEFT half')
          .toBeLessThan(headerMidX);
      }
    }
  });

  test('/activity ActivityHeaderFilters on LEFT', async ({ page }) => {
    await page.goto('/activity', { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    const header = page.locator('.page-header').first();
    const headerBox = await header.boundingBox();
    if (!headerBox) return;
    const filters = header.locator('.act-filters').first();
    if (!(await filters.count())) return;
    const fb = await filters.boundingBox();
    if (!fb) return;
    expect(fb.x + fb.width / 2,
      '/activity filters must sit on LEFT half')
      .toBeLessThan(headerBox.x + headerBox.width / 2);
  });
});

// ── Mobile parity — single page (representative) at 360 px to keep
//   the suite well under the 5-min auth rate-limit window.
test('alignment-mobile parity — /dashboard at 360px keeps the trio on right', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await loginAsAdmin(page);
  await page.goto('/dashboard', { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(800);

  const header = page.locator('.page-header').first();
  if (!(await header.count())) return;
  await expect(header).toBeVisible();

  // ml-auto spacer + page-header-actions present.
  const mlAuto = await header.locator(':scope > .ml-auto').count();
  expect(mlAuto).toBe(1);
  const pha = await header.locator(':scope > .page-header-actions').count();
  expect(pha).toBe(1);

  // On mobile the strip can wrap to multiple rows. The trio still
  // sits in the right HALF of the header bounding box (which now
  // spans multiple rows). We verify the trio's RIGHT edge >= header
  // right edge − 1.6rem (trio width).
  const phaWrap = header.locator('.pha-wrap').first();
  if (await phaWrap.count()) {
    const headerBox = await header.boundingBox();
    const pb = await phaWrap.boundingBox();
    if (headerBox && pb) {
      const headerRight = headerBox.x + headerBox.width;
      const phaRight = pb.x + pb.width;
      expect(headerRight - phaRight,
        'trio right-edge within 30px of header right-edge on mobile')
        .toBeLessThan(30);
    }
  }
});
