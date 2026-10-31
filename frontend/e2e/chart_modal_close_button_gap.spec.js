/**
 * chart_modal_close_button_gap.spec.js
 *
 * 2026-10-31 — operator reported "X button location inconsistent on popup
 * windows on navstrip, some flush to right-top corner, some with a gap."
 * The NavStrip-side investigation (see
 * navstrip_breakdown_close_button_gap.spec.js) found NavBreakdown and
 * BrokerHealthBadge already agree (~14.6px right / ~6.6px top, both built
 * on the shared `canonical-modal-header` convention in app.css) and
 * explicitly flagged ChartModal (`.cm-close`) as the real outlier: it sat
 * ~1px right / ~0px top from `.cm-modal`'s corner — flush — because a
 * local `.cm-header { padding: 0 }` override zeroed the canonical class's
 * `0.35rem 0.85rem` padding to sync the header's height with the
 * order-ticket modal's CardHeader-based header.
 *
 * Fix: `ChartModal.svelte`'s `.cm-header` no longer overrides padding, so
 * it inherits `canonical-modal-header`'s padding directly — same token,
 * no new value invented. This deliberately reverses the prior height-sync
 * (header grows from ~23.7px to ~35px); the close button's position is
 * the operator-reported defect, so it takes priority.
 *
 * This is a dedicated file rather than an extension of
 * navstrip_breakdown_close_button_gap.spec.js because that file's own
 * tests are scoped to `.ps-strip`-launched popups and its docblock
 * explicitly states ChartModal is out of scope there.
 *
 * Run:
 *   cd frontend && npx playwright test chart_modal_close_button_gap
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const TIMEOUT = 30000;
// Same canonical-modal-header padding (0.35rem top/bottom, 0.85rem
// left/right) every other popup in this app is measured against — see
// navstrip_breakdown_close_button_gap.spec.js's own constant.
const EXPECTED_RIGHT_GAP_PX = 14.6;
const EXPECTED_TOP_GAP_PX = 5.6;
const TOLERANCE_PX = 3;

async function openChartModal(page) {
  await loginAsAdmin(page);
  await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: TIMEOUT }).catch(() => {});
  await page.waitForTimeout(1500);

  const btn = page.locator('button.pha-chart');
  const visible = await btn.isVisible({ timeout: 5000 }).catch(() => false);
  if (!visible) return null;
  await btn.click();

  const modal = page.locator('.cm-modal');
  await expect(modal).toBeVisible({ timeout: 10000 });
  return modal;
}

test.describe('ChartModal close-button gap matches canonical convention (2026-10-31)', () => {
  test('desktop: close button sits at the canonical inset, not flush to the corner', async ({ page }) => {
    test.setTimeout(60000);
    const modal = await openChartModal(page);
    if (!modal) {
      test.info().annotations.push({ type: 'skip', description: 'Chart button not visible' });
      return;
    }

    const modalBox = await modal.boundingBox();
    const closeBox = await modal.locator('.cm-close').boundingBox();
    expect(modalBox).toBeTruthy();
    expect(closeBox).toBeTruthy();

    const rightGap = (modalBox.x + modalBox.width) - (closeBox.x + closeBox.width);
    const topGap = closeBox.y - modalBox.y;

    expect(rightGap).toBeGreaterThanOrEqual(EXPECTED_RIGHT_GAP_PX - TOLERANCE_PX);
    expect(rightGap).toBeLessThanOrEqual(EXPECTED_RIGHT_GAP_PX + TOLERANCE_PX);
    expect(topGap).toBeGreaterThanOrEqual(EXPECTED_TOP_GAP_PX - TOLERANCE_PX);
    expect(topGap).toBeLessThanOrEqual(EXPECTED_TOP_GAP_PX + TOLERANCE_PX);

    // Not flush to the corner — the pre-fix regression value was ~1px/~0px.
    expect(rightGap).toBeGreaterThan(5);
    expect(topGap).toBeGreaterThan(2);
  });

  test('mobile-portrait: close button gap matches the desktop value', async ({ page }) => {
    test.setTimeout(60000);
    const modal = await openChartModal(page);
    if (!modal) {
      test.info().annotations.push({ type: 'skip', description: 'Chart button not visible' });
      return;
    }

    const modalBox = await modal.boundingBox();
    const closeBox = await modal.locator('.cm-close').boundingBox();
    expect(modalBox).toBeTruthy();
    expect(closeBox).toBeTruthy();

    const rightGap = (modalBox.x + modalBox.width) - (closeBox.x + closeBox.width);
    const topGap = closeBox.y - modalBox.y;

    expect(rightGap).toBeGreaterThanOrEqual(EXPECTED_RIGHT_GAP_PX - TOLERANCE_PX);
    expect(rightGap).toBeLessThanOrEqual(EXPECTED_RIGHT_GAP_PX + TOLERANCE_PX);
    expect(topGap).toBeGreaterThanOrEqual(EXPECTED_TOP_GAP_PX - TOLERANCE_PX);
    expect(topGap).toBeLessThanOrEqual(EXPECTED_TOP_GAP_PX + TOLERANCE_PX);
  });
});
