// Header button-group standardization (2026-10-13) — Dashboard-specific
// Parts 1 + 3 + 4 + 5 + 7.
//
//   Part 1 — Collapse removed project-wide (covered generically by
//     collapse_check.spec.js; this file does not re-test it).
//   Part 3 — Dashboard ALSO loses Fullscreen entirely (unlike most other
//     migrated pages, which keep it — e.g. /pulse). Scoped assertion:
//     this must NOT be asserted globally.
//   Part 4 — the NAV chart (NavTab.svelte) now actually fills its card's
//     available vertical height (`preserveAspectRatio="none"`'s stretch
//     intent finally takes effect), instead of deriving its height purely
//     from width × a fixed aspect ratio and leaving dead space below it
//     whenever the card is stretched taller than that ratio would produce.
//   Part 5 — the P&L/Capital/Equity tabbed sidebar card gets a visible
//     "Portfolio" header title (previously neither `title` nor `label`).
//   Part 7 — the chart card's outer title ("Chart" → "Trend") is covered
//     by cardheader_sweep_round3_part_cd.spec.js's own updated Part D
//     block, not duplicated here.
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('Dashboard header button-group standardization — Part 3 (Fullscreen removed)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('no Dashboard panel shows a Fullscreen (or DefaultSize) button, scoped to Dashboard only', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    for (const sel of [
      'section.row1-col-chart',
      'section.cap-eq-tabbed',
      'section.dash-agent',
    ]) {
      const card = page.locator(sel);
      await expect(card, `${sel} should be visible`).toBeVisible({ timeout: 20_000 });
      // `.fs-btn` / `.default-btn` are CardControls' real in-place card
      // fullscreen controls — the ones Part 3 removes. The Activity card
      // (section.dash-activity) is deliberately excluded from this loop:
      // it renders a DIFFERENT button (LogPanel's own `.lp-fs-btn`, a
      // deep-link to ActivityLogModal, not a card-portal fullscreen) that
      // predates this whole effort and is shared across four other
      // mount points (/orders, /activity, ActivityLogModal) — out of
      // Part 3's scope, which is about Dashboard's own CardHeader
      // instances (Chart / Capital-Equity / Agent cards) only.
      await expect(card.locator('button.fs-btn'), `${sel} must have no Fullscreen button`).toHaveCount(0);
      await expect(card.locator('button.default-btn'), `${sel} must have no DefaultSize button`).toHaveCount(0);
      await expect(card, `${sel} must never carry fs-card-on`).not.toHaveClass(/fs-card-on/);
    }
  });

  test('Pulse — a sibling page — still shows Fullscreen (negative-control: removal is Dashboard-scoped, not global)', async ({ page }) => {
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('button.fs-btn').first()).toBeVisible({ timeout: 20_000 });
  });
});

test.describe('Dashboard header button-group standardization — Part 4 (NAV chart fills available height)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('desktop: NAV SVG height is driven by its container, not a fixed aspect-ratio (structural check)', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const svg = page.locator('.nav-svg');
    await expect(svg).toBeVisible({ timeout: 20_000 });

    const info = await page.evaluate(() => {
      const s = document.querySelector('.nav-svg');
      const cs = getComputedStyle(s);
      const sBox = s.getBoundingClientRect();
      const frameBox = document.querySelector('.dash-chart-frame').getBoundingClientRect();
      const metaBox = document.querySelector('.nav-tab-meta')?.getBoundingClientRect() ?? null;
      return {
        aspectRatio: cs.aspectRatio,
        cssHeight: cs.height,
        svgH: sBox.height,
        frameH: frameBox.height,
        metaH: metaBox ? metaBox.height : 0,
      };
    });

    // Structural fix (Part 4): the old `aspect-ratio: 760/260` CSS rule
    // is gone — height is no longer locked to width via a fixed ratio.
    expect(info.aspectRatio).toBe('auto');

    // In today's default (unforced) data state the chart card happens to
    // be the taller of the two row siblings, so the SVG's natural height
    // and the old aspect-derived height can legitimately coincide here —
    // the REAL regression guard for "taller sidebar leaves a dead gap
    // below the SVG" is the forced-stretch test below. This assertion
    // only checks the SVG is filling essentially the whole available
    // frame height (not leaving an unexplained gap even in this
    // non-stretched state).
    const available = info.frameH - info.metaH;
    expect(info.svgH).toBeGreaterThan(available - 40);
  });

  test('desktop: NAV SVG grows when the sidebar card is naturally taller than the chart (regression guard for the exact reported bug)', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.nav-svg')).toBeVisible({ timeout: 20_000 });

    // Force the sidebar card artificially taller, simulating a data state
    // where it naturally needs more vertical room than the chart's own
    // historical aspect-ratio-derived height — the exact scenario the
    // operator reported ("nav chart not using available space").
    await page.evaluate(() => {
      document.querySelector('.cap-eq-tabbed').style.minHeight = '650px';
    });
    await page.waitForTimeout(200);

    const { svgH, frameH } = await page.evaluate(() => ({
      svgH: document.querySelector('.nav-svg').getBoundingClientRect().height,
      frameH: document.querySelector('.dash-chart-frame').getBoundingClientRect().height,
    }));
    expect(frameH).toBeGreaterThan(550); // stretched via grid align-items:stretch
    expect(svgH).toBeGreaterThan(frameH - 60); // SVG follows the stretch, no dead gap below it
  });

  test('mobile: NAV SVG keeps its 240px min-height floor', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const svg = page.locator('.nav-svg');
    await expect(svg).toBeVisible({ timeout: 20_000 });
    const box = await svg.boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(239); // 240px floor, 1px rounding tolerance
  });
});

test.describe('Dashboard header button-group standardization — Part 5 (Portfolio title)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('Capital/Equity tabbed card has a visible, non-empty title distinct from its own tab labels', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const card = page.locator('section.cap-eq-tabbed');
    await expect(card).toBeVisible({ timeout: 20_000 });

    const title = card.locator('.ch-title').first();
    await expect(title).toBeVisible({ timeout: 20_000 });
    const text = (await title.textContent())?.trim() ?? '';
    expect(text.length).toBeGreaterThan(0);
    expect(['P&L', 'CAPITAL', 'EQUITY', 'P&l', 'Capital', 'Equity']).not.toContain(text);
    expect(text).toBe('Portfolio');
  });
});
