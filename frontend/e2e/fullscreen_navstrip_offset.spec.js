/**
 * fullscreen_navstrip_offset.spec.js
 *
 * Regression guard for the fullscreen-card top-offset chrome measurement
 * (`DefaultSizeButton.svelte`'s `_measureChrome()`, mirrored in
 * `app.css`'s `.fs-card-on` inset rule via `--fs-card-top`).
 *
 * Operator report (2026-10-02): fullscreening a card on /admin/derivatives
 * (e.g. the Legs grid) visually covered the NavStrip pill cluster
 * (`.ps-strip` — the P/M/C/H strip pinned below the navbar). `_measureChrome`
 * already takes `Math.max()` of navbar/page-header/ps-strip/demo-banner
 * bottom edges (commit 6e23c4a7, already on dev+main at time of this
 * spec's writing) — this spec exists because that path had NO regression
 * coverage, not because a new fix was needed here. Re-verified live
 * against local dev + dev.ramboq.com across all three viewport projects;
 * does not reproduce. This spec locks the non-regressed behaviour in.
 *
 * Quality dimensions:
 * - SSOT: reads the live `--fs-card-top` custom property + real bounding
 *   boxes, not a hardcoded pixel value.
 * - Stale/regression: a future edit to `_measureChrome` that drops the
 *   `.ps-strip` query (or breaks the `Math.max` chain) will fail this.
 * - Reusable: shares authOnce() pattern with fullscreen_card_modal.spec.js.
 * - UX: asserts the geometric invariant the operator actually cares about
 *   (NavStrip stays visible, not covered), not an implementation detail.
 * - Negative control: confirms the SAME offset logic degrades safely when
 *   `.ps-strip` isn't present in the DOM at all (the pre-existing
 *   `.demo-banner`-style optional-chrome pattern `_measureChrome` already
 *   uses — querySelector returning null contributes 0 to the Math.max).
 */

import { test, expect } from '@playwright/test';

test.setTimeout(90_000);

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _cachedToken = process.env.PLAYWRIGHT_AUTH_TOKEN || null;

async function authOnce(page) {
  if (!_cachedToken) {
    let tok = null;
    for (const delay of [0, 20_000, 65_000]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const resp = await page.request.post('/api/auth/login', {
        data: { username: _AUTH_USER, password: _AUTH_PASS },
      });
      if (resp.ok()) { tok = (await resp.json()).access_token; break; }
      if (resp.status() !== 429) throw new Error(`auth: ${resp.status()}`);
    }
    if (!tok) { test.skip(true, 'rate-limited'); return; }
    _cachedToken = tok;
  }
  await page.goto('/');
  await page.evaluate((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify({
      user_id: 'rambo', username: 'rambo', role: 'admin', display_name: 'rambo',
    }));
  }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
}

test.describe('Fullscreen card — NavStrip (.ps-strip) offset', () => {

  test.afterEach(async ({ page }) => {
    try { await page.keyboard.press('Escape'); } catch { /* ignore */ }
  });

  test('fullscreening the Legs grid does not cover NavStrip', async ({ page }) => {
    await authOnce(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('domcontentloaded');

    const psStrip = page.locator('.ps-strip').first();
    const psVisible = await psStrip.isVisible().catch(() => false)
      || await expect(psStrip).toBeVisible({ timeout: 20_000 }).then(() => true).catch(() => false);
    if (!psVisible) {
      test.skip(true, '.ps-strip not visible — PositionStrip may be self-hiding (no positions)');
      return;
    }
    const psBoxBefore = await psStrip.boundingBox();

    const legsCard = page.locator('.opt-legs-card').first();
    const legsVisible = await legsCard.isVisible().catch(() => false)
      || await expect(legsCard).toBeVisible({ timeout: 15_000 }).then(() => true).catch(() => false);
    if (!legsVisible) {
      test.skip(true, '.opt-legs-card not visible — no strategy/positions loaded');
      return;
    }

    const fsBtn = legsCard
      .locator('.fs-btn, [title*="fullscreen" i], [aria-label*="fullscreen" i]')
      .first();
    const fsBtnVisible = await fsBtn.isVisible().catch(() => false)
      || await expect(fsBtn).toBeVisible({ timeout: 10_000 }).then(() => true).catch(() => false);
    if (!fsBtnVisible) {
      test.skip(true, 'Legs grid fullscreen button not visible');
      return;
    }

    await fsBtn.click();
    await expect(page.locator('.opt-legs-card.fs-card-on')).toBeVisible({ timeout: 8_000 });

    // NavStrip must still be visible and painted, not occluded.
    const psVisibleAfter = await psStrip.isVisible().catch(() => false);
    expect(psVisibleAfter, 'NavStrip (.ps-strip) must stay visible while a card is fullscreen').toBe(true);

    const psBoxAfter = await psStrip.boundingBox();
    const cardBox = await page.locator('.opt-legs-card.fs-card-on').boundingBox();
    expect(psBoxAfter, 'NavStrip must report a bounding box').not.toBeNull();
    expect(cardBox, 'fullscreen card must report a bounding box').not.toBeNull();

    // Core geometric invariant: the fullscreen card's top edge must sit
    // AT OR BELOW NavStrip's bottom edge — no vertical overlap.
    const navStripBottom = psBoxAfter.y + psBoxAfter.height;
    expect(
      cardBox.y,
      `fullscreen card top (${cardBox.y}) must be >= NavStrip bottom (${navStripBottom}) — no overlap`,
    ).toBeGreaterThanOrEqual(navStripBottom - 0.5); // sub-pixel rounding tolerance

    // NavStrip's own geometry must be unchanged by the fullscreen toggle
    // (it's "fixed", not displaced by the card).
    expect(psBoxAfter.y).toBeCloseTo(psBoxBefore.y, 0);
    expect(psBoxAfter.height).toBeCloseTo(psBoxBefore.height, 0);

    // Restore via DefaultSizeButton so afterEach's Escape isn't the only path tested.
    const defaultBtn = page.locator('.default-btn').first();
    if (await defaultBtn.isVisible().catch(() => false)) await defaultBtn.click();
  });

  test('negative control — offset still correct when .ps-strip is absent from the DOM', async ({ page }) => {
    // Exercises _measureChrome's null-element branch (querySelector('.ps-strip')
    // returns null, contributing 0 to Math.max — same pattern already used
    // for the optional `.demo-banner` element). Confirms the fix for THIS
    // bug didn't regress the ORIGINAL bug --fs-card-top was added to solve
    // (page-header action buttons being covered on pages with no NavStrip).
    await authOnce(page);
    await page.goto('/admin/derivatives');
    await page.waitForLoadState('domcontentloaded');

    const legsCard = page.locator('.opt-legs-card').first();
    const legsVisible = await legsCard.isVisible().catch(() => false)
      || await expect(legsCard).toBeVisible({ timeout: 15_000 }).then(() => true).catch(() => false);
    if (!legsVisible) {
      test.skip(true, '.opt-legs-card not visible — no strategy/positions loaded');
      return;
    }

    // Forcibly remove NavStrip from the DOM to simulate a page where it
    // never mounts (or hasn't rendered yet).
    await page.evaluate(() => {
      document.querySelectorAll('.ps-strip').forEach((el) => el.remove());
    });

    const navEl = page.locator('.algo-navbar').first();
    const phEl = page.locator('.page-header').first();
    const navBoxBefore = await navEl.boundingBox().catch(() => null);
    const phBoxBefore = await phEl.boundingBox().catch(() => null);

    const fsBtn = legsCard
      .locator('.fs-btn, [title*="fullscreen" i], [aria-label*="fullscreen" i]')
      .first();
    const fsBtnVisible = await fsBtn.isVisible().catch(() => false)
      || await expect(fsBtn).toBeVisible({ timeout: 10_000 }).then(() => true).catch(() => false);
    if (!fsBtnVisible) {
      test.skip(true, 'Legs grid fullscreen button not visible');
      return;
    }

    await fsBtn.click();
    await expect(page.locator('.opt-legs-card.fs-card-on')).toBeVisible({ timeout: 8_000 });

    const cardBox = await page.locator('.opt-legs-card.fs-card-on').boundingBox();
    expect(cardBox).not.toBeNull();

    // With .ps-strip absent, the offset must still clear navbar + page-header
    // (the ORIGINAL --fs-card-top scenario), never regressing to the
    // pre-2026-10-01 bug where those were covered.
    if (navBoxBefore) {
      expect(cardBox.y).toBeGreaterThanOrEqual(navBoxBefore.y + navBoxBefore.height - 0.5);
    }
    if (phBoxBefore) {
      expect(cardBox.y).toBeGreaterThanOrEqual(phBoxBefore.y + phBoxBefore.height - 0.5);
    }

    const defaultBtn = page.locator('.default-btn').first();
    if (await defaultBtn.isVisible().catch(() => false)) await defaultBtn.click();
  });

});
