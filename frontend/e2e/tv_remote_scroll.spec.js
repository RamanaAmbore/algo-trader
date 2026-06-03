/**
 * tv_remote_scroll.spec.js
 *
 * Verifies commit 6a8f5755: arrow / page / home / end keys scroll the
 * focused page or modal body. Simulates a TV remote's D-pad.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/tv_remote_scroll.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken && process.env.PLAYWRIGHT_TOKEN) _cachedToken = process.env.PLAYWRIGHT_TOKEN;
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test.describe('TV-remote scroll (6a8f5755)', () => {
  test.setTimeout(90_000);

  test('ArrowDown / PageDown / End scroll the page; arrows scroll a modal body when modal is open', async ({ page }) => {
    const log = { timeline: [], errors: [] };
    page.on('pageerror', (e) => log.errors.push(String(e).slice(0, 200)));

    await login(page);

    // ── 1. Page-level scroll on /admin/settings (long page, no modal) ──
    await page.goto(`${BASE}/admin/settings`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);

    // Ensure focus is NOT on a form input.
    await page.evaluate(() => {
      const ae = document.activeElement;
      if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) ae.blur();
    });

    const yBefore = await page.evaluate(() => window.scrollY);
    log.timeline.push(`page scrollY before: ${yBefore}`);

    // Tap ArrowDown a few times.
    for (let i = 0; i < 5; i++) await page.keyboard.press('ArrowDown');
    await page.waitForTimeout(500);
    const yAfterArrows = await page.evaluate(() => window.scrollY);
    log.timeline.push(`page scrollY after 5× ArrowDown: ${yAfterArrows}`);
    expect(yAfterArrows).toBeGreaterThan(yBefore);

    // PageDown.
    await page.keyboard.press('PageDown');
    await page.waitForTimeout(500);
    const yAfterPg = await page.evaluate(() => window.scrollY);
    log.timeline.push(`page scrollY after PageDown: ${yAfterPg}`);
    expect(yAfterPg).toBeGreaterThan(yAfterArrows);

    // End jumps to bottom.
    await page.keyboard.press('End');
    await page.waitForTimeout(800);
    const yAfterEnd = await page.evaluate(() => window.scrollY);
    const maxY = await page.evaluate(() => document.scrollingElement.scrollHeight - window.innerHeight);
    log.timeline.push(`page scrollY after End: ${yAfterEnd} of max ${maxY}`);
    expect(yAfterEnd).toBeGreaterThan(yAfterPg);

    // Home back to top.
    await page.keyboard.press('Home');
    await page.waitForTimeout(800);
    const yAfterHome = await page.evaluate(() => window.scrollY);
    log.timeline.push(`page scrollY after Home: ${yAfterHome}`);
    expect(yAfterHome).toBeLessThan(50);
    log.timeline.push('✓ page scroll responds to ArrowDown / PageDown / End / Home');

    // ── 2. Modal-internal scroll ──────────────────────────────────────
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      log.timeline.push('skip modal section: no .pha-order');
      console.log(JSON.stringify(log, null, 2));
      return;
    }
    await orderBtn.click({ force: true });
    await page.waitForTimeout(2000);

    const modal = page.locator('.oes-modal').first();
    expect(await modal.count()).toBeGreaterThan(0);

    // Read modal body initial scrollTop.
    const modalBodyHandle = page.locator('.oes-modal .oes-body').first();
    if (await modalBodyHandle.count() === 0) {
      log.timeline.push('skip modal scroll: no .oes-body');
      console.log(JSON.stringify(log, null, 2));
      return;
    }

    // Move focus off any input inside the modal so our global handler fires.
    await page.evaluate(() => {
      const ae = document.activeElement;
      if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) ae.blur();
      // Focus the modal panel itself.
      const panel = document.querySelector('.oes-modal');
      if (panel && typeof /** @type {HTMLElement} */ (panel).focus === 'function') /** @type {HTMLElement} */ (panel).focus();
    });

    const modalTopBefore = await modalBodyHandle.evaluate((el) => el.scrollTop);
    const modalScrollH = await modalBodyHandle.evaluate((el) => el.scrollHeight);
    const modalClientH = await modalBodyHandle.evaluate((el) => el.clientHeight);
    log.timeline.push(`modal body scrollTop before: ${modalTopBefore} · scrollHeight: ${modalScrollH} · clientHeight: ${modalClientH}`);

    if (modalScrollH <= modalClientH + 4) {
      log.timeline.push('note: modal body has no overflow — scroll assertion skipped (content fits)');
    } else {
      // Send ArrowDown several times.
      for (let i = 0; i < 8; i++) await page.keyboard.press('ArrowDown');
      await page.waitForTimeout(700);
      const modalTopAfter = await modalBodyHandle.evaluate((el) => el.scrollTop);
      log.timeline.push(`modal body scrollTop after 8× ArrowDown: ${modalTopAfter}`);
      expect(modalTopAfter).toBeGreaterThan(modalTopBefore);
      log.timeline.push('✓ modal body scrolls on ArrowDown');
    }

    console.log('\n=== tv-remote-scroll ===');
    console.log(JSON.stringify(log, null, 2));
  });
});
