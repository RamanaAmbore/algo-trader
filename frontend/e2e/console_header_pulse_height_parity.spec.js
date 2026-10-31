/**
 * console_header_pulse_height_parity.spec.js — 2026-10-31.
 *
 * Operator, three follow-up messages treated as one combined ask:
 *   1. "make orders dropdown in automation look similar size and
 *      format like all accounts dropdown in pulse page"
 *   2. "make orders dropdwon and trade guide element of same height
 *      and vertical alaignement"
 *   3. "They should be aligned tab text too"
 *
 * This is a further refinement of the alignment work already covered
 * by `console_ui_polish_bundle.spec.js` (search for `aw-kind-select`),
 * which pinned the dropdown/Guide-link pair to the Orders CLI tab's
 * 1.7rem floor. That reference is now superseded by Pulse's
 * AccountMultiSelect (`MultiSelect.svelte`'s `.rbq-multi-trigger`,
 * min-height 1.55rem) — see the dated 2026-10-31 comment above
 * `.console-guide-btn` in `+page.svelte` for the full box-model
 * derivation (Select.svelte's own unoverridden trigger and
 * MultiSelect's trigger already share one box-model convention
 * byte-for-byte, so "match Pulse" and "use Select's natural size" are
 * the same target).
 *
 * This spec adds the THIRD leg the polish-bundle spec didn't cover:
 * the AutomationTabs tab-strip text must also read as the same height
 * / vertical center as the dropdown and Guide link, not just the
 * dropdown vs. the Guide link pairwise. It also checks TEXT centers
 * (not just box centers) — `.algo-tab` carries a 2px bottom border and
 * `margin-bottom: -1px`, which can put its box center up to ~0.5px off
 * its own text's visual center; comparing box centers alone would miss
 * that.
 *
 * No `test.use({ projects: [...] })` restriction — confirmed during
 * this round that pattern is NOT a real Playwright option (it's a
 * silently-ignored fixture key; running the sibling spec with an
 * explicit `--project=mobile-portrait` override still executes it).
 * This spec runs on the real configured projects (chromium-desktop
 * 1400px, mobile-portrait 360px, mobile-landscape 800px) and adapts
 * its expected height to the actual viewport width instead.
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 20_000;

/** Returns the bounding rect of the first non-whitespace text node inside `el`. */
function textCenterScript() {
  return () => {
    function textCenterY(el) {
      if (!el) return null;
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let node = null;
      while (walker.nextNode()) {
        if (walker.currentNode.textContent.trim().length) { node = walker.currentNode; break; }
      }
      if (!node) {
        const r = el.getBoundingClientRect();
        return r.top + r.height / 2;
      }
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      return r.top + r.height / 2;
    }
    const select = document.querySelector('.aw-kind-select .rbq-select-label');
    const guide = document.querySelector('.console-guide-btn');
    const consoleTab = Array.from(document.querySelectorAll('.algo-tabs-strip .algo-tab'))
      .find((b) => b.textContent.trim().toLowerCase() === 'console');
    return {
      selectTextCenterY: textCenterY(select),
      guideTextCenterY: textCenterY(guide),
      tabTextCenterY: textCenterY(consoleTab),
    };
  };
}

test('kind-dropdown, Trader Guide link, and the CONSOLE tab text are all within ~1px of the same vertical center', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  const select = page.locator('.aw-kind-select .rbq-select-trigger');
  await expect(select).toBeVisible({ timeout: TIMEOUT });

  const centers = await page.evaluate(textCenterScript());
  expect(centers.selectTextCenterY).not.toBeNull();
  expect(centers.guideTextCenterY).not.toBeNull();
  expect(centers.tabTextCenterY).not.toBeNull();

  // Select/Guide must match each other to sub-pixel precision — same
  // invariant the polish-bundle spec already pins via box centers.
  expect(Math.abs(centers.selectTextCenterY - centers.guideTextCenterY)).toBeLessThanOrEqual(1);
  // The tab strip's OWN box model (2px border + -1px margin, both
  // owned by the shared, cross-page `.algo-tab` class — out of scope
  // to edit here) keeps its text center within ~1px of Select/Guide's,
  // not byte-identical. This is the operator's 3rd follow-up ("aligned
  // tab text too") and the tolerance the task's own verification brief
  // calls for (1-2px).
  expect(Math.abs(centers.tabTextCenterY - centers.selectTextCenterY)).toBeLessThanOrEqual(2);
  expect(Math.abs(centers.tabTextCenterY - centers.guideTextCenterY)).toBeLessThanOrEqual(2);
});

test('kind-dropdown, Trader Guide link, and the tab strip stay within ~2px of each other in rendered height', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  const select = page.locator('.aw-kind-select .rbq-select-trigger');
  const guide = page.locator('.console-guide-btn');
  const tabsWrap = page.locator('.aw-tabs-in-header');
  await expect(select).toBeVisible({ timeout: TIMEOUT });

  const [selectBox, guideBox, tabsWrapBox] = await Promise.all([
    select.boundingBox(),
    guide.boundingBox(),
    tabsWrap.boundingBox(),
  ]);

  // Select and Guide move together exactly (both carry the identical
  // `max(1.55rem, var(--toolbar-h))` floor) — tight 1px tolerance.
  expect(Math.abs(selectBox.height - guideBox.height)).toBeLessThanOrEqual(1);
  // The tab strip's own wrapper height is driven by the shared
  // `.algo-tab` class's `min-height: var(--toolbar-h)` (24px desktop,
  // 28px mobile — a global SSOT also used by chart toolbars elsewhere,
  // out of scope to edit here), one token short of Select/Guide's own
  // `max(1.55rem, var(--toolbar-h))` floor by construction on desktop
  // (24px vs ~24.8px) — within the task's stated 1-2px tolerance.
  expect(Math.abs(selectBox.height - tabsWrapBox.height)).toBeLessThanOrEqual(2);
  expect(Math.abs(guideBox.height - tabsWrapBox.height)).toBeLessThanOrEqual(2);
});

test('kind-dropdown height matches Pulse AccountMultiSelect\'s own box-model convention at the real viewport width', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/automation?tab=console', { waitUntil: 'domcontentloaded' });
  const select = page.locator('.aw-kind-select .rbq-select-trigger');
  await expect(select).toBeVisible({ timeout: TIMEOUT });

  const selectBox = await select.boundingBox();
  const vpWidth = page.viewportSize()?.width ?? 1400;
  if (vpWidth <= 600) {
    // ≤600px: `--toolbar-h` (28px) governs via the `max()` floor — the
    // dropdown/Guide tap target stays at the platform's mobile height
    // rather than shrinking to Pulse's flat 24.8px, by deliberate
    // design (see the 2026-10-31 comment in +page.svelte).
    expect(selectBox.height).toBeGreaterThan(27);
    expect(selectBox.height).toBeLessThan(29);
  } else {
    // >600px: Pulse AccountMultiSelect's own 1.55rem floor governs —
    // confirmed live: 24.796875px.
    expect(selectBox.height).toBeGreaterThan(23);
    expect(selectBox.height).toBeLessThan(26);
  }
});
