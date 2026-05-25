// @ts-check
// Quick targeted check for the mobile swipe hint CSS pseudo-element
// and other remaining checks from the recheck task.

import { test, expect } from '@playwright/test';

const AUTH_USER = process.env.PLAYWRIGHT_USER || 'ambore';
const AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'Zerodha01#';

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('#s-user, input[name="username"], input[type="text"]').first().fill(AUTH_USER);
  await page.locator('#s-pass, input[name="password"], input[type="password"]').first().fill(AUTH_PASS);
  await page.locator('button.btn-primary, button[type="submit"]').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 20000 });
  await page.waitForFunction(() => !!sessionStorage.getItem('ramboq_token'), { timeout: 10000 }).catch(() => {});
}

test.describe.serial('perf — targeted checks', () => {

  test('mobile 390×844 — swipe hint CSS pseudo-element + NavCard centering', async ({ page }, testInfo) => {
    page.setViewportSize({ width: 390, height: 844 });
    await signIn(page);
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.section-heading, .nav-card', { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(1500);

    // ── Swipe hint via CSS ::after computed content ──
    // At ≤600px, .section-heading::after should have content ' · swipe →'
    const swipeContent = await page.evaluate(() => {
      const h2 = document.querySelector('.section-heading');
      if (!h2) return null;
      return getComputedStyle(h2, '::after').content;
    });
    console.log('[swipe ::after content]', swipeContent);
    // Also check funds heading specifically
    const fundsSwipeContent = await page.evaluate(() => {
      const el = document.querySelector('.funds-heading-title');
      if (!el) return null;
      return getComputedStyle(el, '::after').content;
    });
    console.log('[funds-heading-title ::after content]', fundsSwipeContent);

    // ── NavCard FIRM NAV centering on mobile ──
    // Single panel (.nav-panel-firm) — check text-align
    const firmPanelTA = await page.evaluate(() => {
      const el = document.querySelector('.nav-panel-firm');
      if (!el) return null;
      return getComputedStyle(el).textAlign;
    });
    console.log('[.nav-panel-firm text-align]', firmPanelTA);

    const firmPanelAI = await page.evaluate(() => {
      const el = document.querySelector('.nav-panel-firm');
      if (!el) return null;
      return getComputedStyle(el).alignItems;
    });
    console.log('[.nav-panel-firm align-items]', firmPanelAI);

    // NavCard single-panel centering via :not(.nav-two-panels) > .nav-panel
    const navPanelMargin = await page.evaluate(() => {
      const el = document.querySelector('.nav-panel');
      if (!el) return null;
      const cs = getComputedStyle(el);
      return { ml: cs.marginLeft, mr: cs.marginRight, maxW: cs.maxWidth };
    });
    console.log('[.nav-panel margin]', navPanelMargin);

    // ── Strategy strip text at mobile ──
    const strategyEl = page.locator('.perf-strategy');
    const stratVisible = await strategyEl.isVisible().catch(() => false);
    console.log('[.perf-strategy visible at mobile]', stratVisible);
    if (stratVisible) {
      const text = (await strategyEl.textContent()).trim();
      console.log('[strategy text]', text.slice(0, 120));
    }

    // ── NavCard two-panel class check ──
    const hasTwoPanels = await page.evaluate(() => !!document.querySelector('.nav-two-panels'));
    console.log('[nav-two-panels class present]', hasTwoPanels);
    // With ambore (share_pct=0 admin), only FIRM NAV shows → single panel
    console.log('[expected: false for ambore (share_pct=0)]');

    // Screenshot
    const ssPath = testInfo.outputPath('perf-mobile-swipe.png');
    await page.screenshot({ path: ssPath, fullPage: false });
    await testInfo.attach('mobile-swipe-check', { path: ssPath, contentType: 'image/png' });
    console.log('[screenshot]', ssPath);
  });

  test('desktop 1366×768 — strategy strip class + FIRM NAV centering', async ({ page }, testInfo) => {
    page.setViewportSize({ width: 1366, height: 768 });
    await signIn(page);
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.perf-strategy, .nav-card', { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(1500);

    // ── Strategy strip actual class ──
    const stratViaClass = await page.locator('.perf-strategy').isVisible().catch(() => false);
    console.log('[.perf-strategy visible (desktop)]', stratViaClass);

    const stratText = stratViaClass ? (await page.locator('.perf-strategy').textContent()).trim() : '';
    console.log('[strategy text]', stratText.slice(0, 150));

    // Check all three sub-spans
    const lblText = await page.locator('.perf-strategy-lbl').textContent().catch(() => '');
    const valText = await page.locator('.perf-strategy-val').textContent().catch(() => '');
    const metaText= await page.locator('.perf-strategy-meta').textContent().catch(() => '');
    console.log('[strategy lbl]', lblText.trim());
    console.log('[strategy val]', valText.trim());
    console.log('[strategy meta]', metaText.trim());

    // ── FIRM NAV centering ──
    const firmTA = await page.evaluate(() => {
      const el = document.querySelector('.nav-panel-firm');
      return el ? getComputedStyle(el).textAlign : null;
    });
    const firmAI = await page.evaluate(() => {
      const el = document.querySelector('.nav-panel-firm');
      return el ? getComputedStyle(el).alignItems : null;
    });
    console.log('[firm-nav text-align (desktop)]', firmTA);
    console.log('[firm-nav align-items (desktop)]', firmAI);

    // ── Vertical order sanity ──
    const stratBox   = await page.locator('.perf-strategy').boundingBox().catch(() => null);
    const navBox     = await page.locator('.nav-card').first().boundingBox().catch(() => null);
    const tabsBox    = await page.locator('.tabs-row').first().boundingBox().catch(() => null);
    console.log('[order: strategy.y]', stratBox?.y?.toFixed(0));
    console.log('[order: navcard.y]',  navBox?.y?.toFixed(0));
    console.log('[order: tabs.y]',     tabsBox?.y?.toFixed(0));
    // Expect: strategy < navcard < tabs
    if (stratBox && navBox && tabsBox) {
      console.log('[order correct (strat<nav<tabs)]', stratBox.y < navBox.y && navBox.y < tabsBox.y);
    }

    // Screenshot
    const ssPath = testInfo.outputPath('perf-desktop-targeted.png');
    await page.screenshot({ path: ssPath, fullPage: false });
    await testInfo.attach('desktop-targeted', { path: ssPath, contentType: 'image/png' });
    console.log('[screenshot]', ssPath);
  });

});
