/**
 * scroll_x_utility_refactor.spec.js
 *
 * Regression coverage for the `.scroll-x` CSS utility extraction (app.css) —
 * dedupes the identical scrollbar-hiding/touch-scroll mechanics
 * (`overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width:
 * none;` + `::-webkit-scrollbar { display: none; }`) that was previously
 * hand-duplicated across 12 components. This spec is representative
 * coverage, not exhaustive — the footer's own existing spec
 * (footer_credits.spec.js) and OrderBook's own existing spec
 * (order_book_filled_predicate_and_mobile_overflow.spec.js) already prove
 * the refactor is behavior-neutral for those two surfaces end-to-end.
 *
 * Covers:
 *   (b) OrderBook's `.ob-status-bar` and AlgoTimestamp's `.ats-scroll` are
 *       still genuinely scrollable and non-wrapping after the class change
 *       (computed-style parity check, narrower than the existing full specs).
 *   (c) PnlAnalysis's `.legend-chip` no longer wraps internally at a narrow
 *       viewport (incidental fix — `flex-shrink: 0; white-space: nowrap`
 *       added alongside the dedup).
 *   (d) MarketPulse's `.mp-head-tabs` has exactly one scroll container, not
 *       a nested double-scroll pair with the embedded AlgoTabs' own
 *       `.algo-tabs-strip` (incidental fix — the inner strip is neutralised
 *       via `overflow-x: visible; max-width: none; flex-shrink: 0`,
 *       matching LogPanel.svelte's existing equivalent override).
 *
 * Run:
 *   npx playwright test scroll_x_utility_refactor --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

test.describe('.scroll-x utility — representative regression coverage', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('(b) OrderBook .ob-status-bar: still a real horizontal scroller with hidden scrollbar', async ({ page }) => {
    await page.setViewportSize({ width: 300, height: 800 });
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    const bar = page.locator('.ob-status-bar').first();
    await expect(bar).toBeAttached({ timeout: TIMEOUT });

    const style = await bar.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        overflowX: cs.overflowX,
        scrollbarWidth: cs.scrollbarWidth,
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
      };
    });
    expect(style.overflowX).toBe('auto');
    expect(style.scrollbarWidth).toBe('none');
    // 6 status chips at a 360px viewport — the row must genuinely overflow
    // its own box so the scroll mechanics are exercised, not merely present.
    expect(style.scrollWidth).toBeGreaterThan(style.clientWidth);
  });

  test('(b) AlgoTimestamp .ats-scroll: still a real horizontal scroller with hidden scrollbar', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 800 });
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const scroller = page.locator('.ats-scroll').first();
    await expect(scroller).toBeAttached({ timeout: TIMEOUT });

    const style = await scroller.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { overflowX: cs.overflowX, scrollbarWidth: cs.scrollbarWidth };
    });
    expect(style.overflowX).toBe('auto');
    expect(style.scrollbarWidth).toBe('none');
  });

  test('(c) PnlAnalysis .legend-chip does not wrap internally at a narrow viewport', async ({ page }) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: 360, height: 800 });
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const perfTab = page.getByRole('tab', { name: 'Performance' }).first();
    await expect(perfTab).toBeVisible({ timeout: TIMEOUT });
    await perfTab.click();

    const legendRow = page.locator('.legend-row').first();
    await expect(legendRow).toBeAttached({ timeout: TIMEOUT });

    const chips = legendRow.locator('.legend-chip');
    await expect(chips.first()).toBeAttached({ timeout: TIMEOUT });
    const chipCount = await chips.count();
    expect(chipCount).toBeGreaterThan(0);

    const chipStyles = await legendRow.evaluate((row) => {
      return Array.from(row.querySelectorAll('.legend-chip')).map((el) => {
        const cs = getComputedStyle(el);
        return {
          flexShrink: cs.flexShrink,
          whiteSpace: cs.whiteSpace,
          // A wrapped chip's rendered height exceeds a single line's height
          // (the .legend-dot + text would stack instead of sitting inline).
          height: el.getBoundingClientRect().height,
        };
      });
    });
    for (const s of chipStyles) {
      expect(s.flexShrink).toBe('0');
      expect(s.whiteSpace).toBe('nowrap');
    }
    // Every chip is the same single-line height — none has silently grown
    // taller from internal wrapping.
    const heights = chipStyles.map((s) => Math.round(s.height));
    expect(new Set(heights).size).toBe(1);
  });

  test('(d) MarketPulse .mp-head-tabs is the single scroll container, not a nested double-scroll pair', async ({ page }) => {
    test.setTimeout(60_000);
    // Narrow enough that the embedded AlgoTabs (Underlying / Large Cap /
    // Midcap / Smallcap) genuinely overflows mp-head-tabs's own width,
    // so this test exercises real overflow, not just a static CSS property.
    // /pulse (not /dashboard) — the Gainers/Losers bucket headers that own
    // .mp-head-tabs render unconditionally on /pulse (showWinners/showLosers
    // default true); /dashboard's MarketPulse summary embed doesn't reliably
    // include the movers buckets.
    await page.setViewportSize({ width: 320, height: 800 });
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const headTabs = page.locator('.mp-head-tabs').first();
    await expect(headTabs).toBeAttached({ timeout: TIMEOUT });

    const result = await headTabs.evaluate((el) => {
      const subtree = [el, ...el.querySelectorAll('*')];
      // A genuine scroll container clips real overflowing content
      // (scrollWidth > clientWidth) AND resolves to a non-'visible'
      // overflow-x. The neutralised inner .algo-tabs-strip also computes
      // overflow-x: 'auto' (CSS Overflow spec: 'visible' on one axis
      // combined with a non-'visible' value on the other axis — its own
      // overflow-y: hidden — computes to 'auto'), so overflow-x alone
      // can't distinguish it; scrollWidth > clientWidth is what matters —
      // the neutralised strip has nothing left to clip internally once
      // max-width:none/flex-shrink:0 let it grow to its natural width.
      const scrollers = subtree.filter((node) => {
        const cs = getComputedStyle(node);
        return cs.overflowX !== 'visible' && node.scrollWidth > node.clientWidth;
      });
      return {
        scrollerCount: scrollers.length,
        scrollerClasses: scrollers.map((n) => n.className),
      };
    });

    expect(result.scrollerCount, `expected exactly one scroll container, found: ${JSON.stringify(result.scrollerClasses)}`).toBe(1);
    // The one genuinely-scrolling element must be .mp-head-tabs itself —
    // the embedded AlgoTabs' own .algo-tabs-strip must be neutralised
    // (nothing left for IT to clip), not a second nested scroller.
    expect(result.scrollerClasses[0]).toContain('mp-head-tabs');
  });
});
