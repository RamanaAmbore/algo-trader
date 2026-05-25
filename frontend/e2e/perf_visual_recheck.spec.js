// @ts-check
//
// Visual recheck for /performance after: Pulse hierarchy refactor,
// FIRM NAV centering, strategy strip, mobile swipe hint, Accounts/Symbols
// label trim.
//
// Run:
//   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
//   npx playwright test e2e/perf_visual_recheck.spec.js \
//     --project=chromium-desktop --headed

import { test, expect } from '@playwright/test';

const AUTH_USER = process.env.PLAYWRIGHT_USER || 'ambore';
const AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'Zerodha01#';

/** Drive the real /signin form. Returns when redirected away from /signin. */
async function signIn(page, user = AUTH_USER, pass = AUTH_PASS) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('#s-user, input[name="username"], input[type="text"]').first().fill(user);
  await page.locator('#s-pass, input[name="password"], input[type="password"]').first().fill(pass);
  await page.locator('button.btn-primary, button[type="submit"]').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 20000 });
  // Give the auth store a moment to populate sessionStorage
  await page.waitForFunction(() => !!sessionStorage.getItem('ramboq_token'), { timeout: 10000 }).catch(() => {});
}

// ── All tests serial to avoid burning the 5/min auth rate limit ──────────────
test.describe.serial('/performance visual recheck (dev.ramboq.com)', () => {

  // ── 1. Anonymous on dev: /performance renders as public page (no auth required) ──
  test('1. anonymous on dev — /performance renders as public page', async ({ page }, testInfo) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    // On dev, /performance is a public route — stays at /performance (no redirect).
    // The page should render with data grids and strategy strip.
    const url = page.url();
    console.log('[anon url]', url);
    // Sign In button should be visible (anonymous → no user pill)
    const signInVisible = await page.getByRole('link', { name: /sign in/i }).first().isVisible().catch(() =>
      page.getByText(/sign in/i).first().isVisible().catch(() => false)
    );
    console.log('[anon sign-in button visible]', signInVisible);

    // Strategy strip should be visible even for anonymous
    await page.waitForSelector('.ag-row, [class*="strategy"]', { timeout: 15000 }).catch(() => {});
    const gridRows = await page.locator('.ag-row').count();
    console.log('[anon grid rows]', gridRows);

    const ssPath = testInfo.outputPath('perf-anon.png');
    await page.screenshot({ path: ssPath, fullPage: false });
    await testInfo.attach('anon-view', { path: ssPath, contentType: 'image/png' });

    // /performance stays at /performance (it is a public page)
    expect(url).toContain('/performance');
    // Grid rows — may be zero when market is closed / no data yet; just log
    // (not a hard assertion since this is an informational check)
    console.log('[anon grid rows (may be 0 when closed)]', gridRows);
  });

  // ── 2. Desktop layout checks ─────────────────────────────────────────────
  test('2. desktop (1366×768) — full layout after signin', async ({ page }, testInfo) => {
    page.setViewportSize({ width: 1366, height: 768 });

    // Collect console errors
    const consoleErrors = [];
    page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
    page.on('pageerror', err => consoleErrors.push(`[pageerror] ${err.message}`));

    await signIn(page);
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });

    // Wait for content to settle — any grid row or strategy strip or nav-card
    await page.waitForSelector('.ag-row, .perf-strategy-strip, [class*="strategy-strip"], [class*="nav-card"]', { timeout: 20000 }).catch(() => {});
    // Extra settle time for grids + reactivity
    await page.waitForTimeout(2000);

    // ── Strategy strip ──
    const stripSel = '.perf-strategy-strip, [class*="strategy-strip"], [class*="strat-strip"]';
    const strip = page.locator(stripSel).first();
    const stripVisible = await strip.isVisible().catch(() => false);
    let stripText = '';
    if (stripVisible) {
      stripText = (await strip.textContent()).trim();
    } else {
      // Fallback: look for text containing "Strategy:"
      const fallback = page.getByText(/^Strategy:/).first();
      const fVisible = await fallback.isVisible().catch(() => false);
      if (fVisible) stripText = (await fallback.textContent()).trim();
    }
    console.log('[strategy-strip visible]', stripVisible);
    console.log('[strategy-strip text]', stripText || '<not found>');

    // ── NavCard ──
    const navCard = page.locator('.nav-card, [class*="navcol"], [class*="nav-card"]').first();
    const navCardVisible = await navCard.isVisible().catch(() => false);
    console.log('[navcard visible]', navCardVisible);

    let firmNavTA = '';
    if (navCardVisible) {
      // FIRM NAV centering — try multiple class patterns
      const firmSelectors = ['.firm-nav-panel', '[class*="firm-nav"]', '[class*="navcol-firm"]', '[class*="nav-right"]'];
      for (const sel of firmSelectors) {
        const el = page.locator(sel).first();
        if (await el.isVisible().catch(() => false)) {
          firmNavTA = await el.evaluate(el => getComputedStyle(el).textAlign);
          console.log(`[firm-nav sel="${sel}" text-align="${firmNavTA}"]`);
          break;
        }
      }
      if (!firmNavTA) console.log('[firm-nav] no panel found — checking all children text-align');
    }

    // ── Tabs ──
    const posTab = page.getByRole('button', { name: 'Positions' });
    const holdTab = page.getByRole('button', { name: 'Holdings' });
    const posTabVisible = await posTab.isVisible().catch(() => false);
    const holdTabVisible = await holdTab.isVisible().catch(() => false);
    console.log('[tab Positions visible]', posTabVisible);
    console.log('[tab Holdings visible]', holdTabVisible);

    // ── Account/symbol label text ──
    // "All Accounts" should NOT appear; "Accounts" should
    const allAccountsCount = await page.getByText('All Accounts', { exact: true }).count();
    const allSymbolsCount = await page.getByText('All Symbols', { exact: true }).count();
    const accountsLabelCount = await page.getByText('Accounts', { exact: true }).count();
    const symbolsLabelCount = await page.getByText('Symbols', { exact: true }).count();
    console.log('[All Accounts count (want 0)]', allAccountsCount);
    console.log('[All Symbols count (want 0)]', allSymbolsCount);
    console.log('[Accounts label count]', accountsLabelCount);
    console.log('[Symbols label count]', symbolsLabelCount);

    // ── Grids ──
    const gridRows = await page.locator('.ag-row').count();
    console.log('[ag-grid rows]', gridRows);

    // ── Vertical order: strip → navcard → tabs ──
    const stripBox = stripVisible ? await strip.boundingBox() : null;
    const navCardBox = navCardVisible ? await navCard.boundingBox() : null;
    const posTabBox = posTabVisible ? await posTab.boundingBox() : null;
    console.log('[strip.y]', stripBox?.y ?? 'n/a');
    console.log('[navcard.y]', navCardBox?.y ?? 'n/a');
    console.log('[tabs.y]', posTabBox?.y ?? 'n/a');

    // ── Overflow ──
    const hasHorizScroll = await page.evaluate(() =>
      document.documentElement.scrollWidth > document.documentElement.clientWidth
    );
    console.log('[horizontal scroll]', hasHorizScroll);

    // ── Console errors ──
    console.log('[console errors]', consoleErrors.length, consoleErrors.slice(0, 5));

    // ── Screenshot ──
    const ssPath = testInfo.outputPath('perf-desktop.png');
    await page.screenshot({ path: ssPath, fullPage: true });
    await testInfo.attach('desktop-signed-in', { path: ssPath, contentType: 'image/png' });
    console.log('[screenshot]', ssPath);

    // Assertions
    expect(posTabVisible, 'Positions tab visible').toBe(true);
    expect(holdTabVisible, 'Holdings tab visible').toBe(true);
    expect(gridRows, 'At least one ag-Grid row').toBeGreaterThan(0);
    expect(allAccountsCount, '"All Accounts" text should not appear').toBe(0);
    expect(allSymbolsCount, '"All Symbols" text should not appear').toBe(0);
    expect(hasHorizScroll, 'No horizontal overflow on desktop').toBe(false);
  });

  // ── 3. Mobile layout ─────────────────────────────────────────────────────
  test('3. mobile (390×844) — layout checks after signin', async ({ page }, testInfo) => {
    page.setViewportSize({ width: 390, height: 844 });

    await signIn(page);
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.ag-row, [class*="strategy"], [class*="nav-card"]', { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(2000);

    // ── Swipe hint ──
    // Section headings should carry '· swipe →' or similar at ≤600px
    const swipeHints = await page.getByText(/swipe/i).count();
    console.log('[mobile swipe hints]', swipeHints);

    // ── Tabs ──
    const posTabVisible = await page.getByRole('button', { name: 'Positions' }).isVisible().catch(() => false);
    const holdTabVisible = await page.getByRole('button', { name: 'Holdings' }).isVisible().catch(() => false);
    console.log('[mobile Positions tab]', posTabVisible);
    console.log('[mobile Holdings tab]', holdTabVisible);

    // ── Label trim ──
    const allAccountsCount = await page.getByText('All Accounts', { exact: true }).count();
    const allSymbolsCount = await page.getByText('All Symbols', { exact: true }).count();
    console.log('[mobile All Accounts (want 0)]', allAccountsCount);
    console.log('[mobile All Symbols (want 0)]', allSymbolsCount);

    // ── Overflow ──
    const hasHorizScroll = await page.evaluate(() =>
      document.documentElement.scrollWidth > document.documentElement.clientWidth
    );
    console.log('[mobile horizontal scroll]', hasHorizScroll);

    // ── NavCard stacked ──
    const navCard = page.locator('.nav-card, [class*="nav-card"]').first();
    const navCardVisible = await navCard.isVisible().catch(() => false);
    if (navCardVisible) {
      const navCardWidth = await navCard.evaluate(el => el.offsetWidth);
      console.log('[mobile navcard width]', navCardWidth, '(viewport: 390)');
      // In stacked mode the card should span at least 80% of viewport
      console.log('[mobile navcard stacked?]', navCardWidth >= 280);
    } else {
      console.log('[mobile navcard not visible]');
    }

    // ── Screenshot ──
    const ssPath = testInfo.outputPath('perf-mobile.png');
    await page.screenshot({ path: ssPath, fullPage: true });
    await testInfo.attach('mobile-signed-in', { path: ssPath, contentType: 'image/png' });
    console.log('[screenshot]', ssPath);

    // Assertions
    expect(posTabVisible, 'Positions tab on mobile').toBe(true);
    expect(holdTabVisible, 'Holdings tab on mobile').toBe(true);
    expect(allAccountsCount, '"All Accounts" should not appear on mobile').toBe(0);
    expect(allSymbolsCount, '"All Symbols" should not appear on mobile').toBe(0);
    expect(hasHorizScroll, 'No horizontal overflow on mobile').toBe(false);
  });

});
