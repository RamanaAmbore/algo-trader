/**
 * Visual headed check: audience-routing redesign.
 *
 * Checks 1, 5, 6 run against dev.ramboq.com (public routes, no auth).
 * Checks 2, 3, 4 run against ramboq.com (prod) because the demo experience
 * (isDemo = anonymous + branch='main') only fires on the main branch.
 *   - Check 2: /dashboard as anonymous → demo banner
 *   - Check 3: /showcase via "Take the tour" link in the demo banner
 *   - Check 4: /admin/tokens as anonymous → read-only state
 *
 * All prod checks are READ-ONLY — no writes, no broker actions.
 *
 * Run with:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   cd frontend && npx playwright test e2e/audience_routing_visual.spec.js \
 *     --headed --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';

const DEV_URL  = 'https://dev.ramboq.com';
const PROD_URL = 'https://ramboq.com';
const STRATEGY_HERO = 'Long-term stock investments paired with a toolkit of algo-executed options strategies';
const SCREENSHOT_DIR = '/tmp';

const consoleErrors = [];

test.describe('Audience-routing redesign', () => {

  // ── CHECK 1 — Anonymous home page (dev.ramboq.com) ─────────────────

  test('1 · Anonymous home page — hero, trust strip, Y-fork, partnership grid, navbar button', async ({ page }) => {
    page.on('console', msg => msg.type() === 'error' && consoleErrors.push({ url: page.url(), text: msg.text() }));
    page.on('pageerror', err => consoleErrors.push({ url: page.url(), text: `UNCAUGHT: ${err.message}` }));

    await page.goto(DEV_URL + '/', { waitUntil: 'domcontentloaded' });

    // If deploy is still rolling, wait up to 90 s for the new hero text
    for (let attempt = 0; attempt < 9; attempt++) {
      const heroVisible = await page.getByText(STRATEGY_HERO, { exact: false }).isVisible().catch(() => false);
      if (heroVisible) break;
      await page.waitForTimeout(10000);
      await page.reload({ waitUntil: 'domcontentloaded' });
    }

    // 1a — Hero paragraph contains the strategy framing
    await expect(page.getByText(STRATEGY_HERO, { exact: false })).toBeVisible();

    // 1b — Trust strip: 4 cells
    await expect(page.getByText('22%', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('25+', { exact: false }).first()).toBeVisible();
    await expect(page.getByText(/FRM.*CFA|CFA.*XLRI/i).first()).toBeVisible();
    await expect(page.getByText(/ACU-5195/i).first()).toBeVisible();

    // 1c — Two Y-fork cards
    await expect(page.getByText('Partner with us').first()).toBeVisible();
    await expect(page.getByText('Open the platform').first()).toBeVisible();

    // 1d — Partnership Terms chips
    await expect(page.getByText('₹10 lakh').first()).toBeVisible();
    await expect(page.getByText('Stocks + options overlay').first()).toBeVisible();

    // 1e — Navbar button reads "Platform Demo ↗" (NOT "Algo Site Demo")
    const navBtn = page.getByRole('button', { name: /Platform Demo/i }).first();
    await expect(navBtn).toBeVisible();
    const navBtnText = await navBtn.textContent();
    expect(navBtnText).not.toMatch(/Algo Site Demo/i);

    await page.screenshot({ path: `${SCREENSHOT_DIR}/home-anon.png`, fullPage: true });
  });

  // ── CHECKS 2–4 — Demo mode (prod/main branch) ──────────────────────
  // These run against ramboq.com where isDemo=true for anonymous visitors.

  test('2 · Dashboard demo mode — purple banner with "Live production platform" text', async ({ page }) => {
    page.on('console', msg => msg.type() === 'error' && consoleErrors.push({ url: page.url(), text: msg.text() }));
    page.on('pageerror', err => consoleErrors.push({ url: page.url(), text: `UNCAUGHT: ${err.message}` }));

    // Anonymous visit to prod dashboard → demo mode
    await page.goto(PROD_URL + '/dashboard', { waitUntil: 'domcontentloaded' });

    // Wait for the page to settle (paperStatus poll returns branch='main')
    await page.waitForTimeout(3000);

    // Demo banner should render: "Live production platform · real broker data · accounts masked · paper-only writes."
    const demoBanner = page.locator('.demo-banner');
    await expect(demoBanner).toBeVisible({ timeout: 10000 });
    await expect(demoBanner).toContainText('Live production platform');
    await expect(demoBanner).toContainText('real broker data');
    await expect(demoBanner).toContainText('accounts masked');
    await expect(demoBanner).toContainText('paper-only writes');

    // "Take the tour" and "Sign in" links inside the banner
    await expect(demoBanner.getByRole('link', { name: /Take the tour/i })).toBeVisible();
    await expect(demoBanner.getByRole('link', { name: /Sign in/i })).toBeVisible();

    await page.screenshot({ path: `${SCREENSHOT_DIR}/dashboard-demo.png`, fullPage: false });
  });

  test('3 · /showcase — hero toolkit text, 6 fact chips, 6 architecture cards', async ({ page }) => {
    page.on('console', msg => msg.type() === 'error' && consoleErrors.push({ url: page.url(), text: msg.text() }));
    page.on('pageerror', err => consoleErrors.push({ url: page.url(), text: `UNCAUGHT: ${err.message}` }));

    // Navigate to prod /showcase directly (linked from the demo banner's "Take the tour")
    await page.goto(PROD_URL + '/showcase', { waitUntil: 'domcontentloaded' });

    // 3a — Hero paragraph mentions the options strategy toolkit
    const heroTag = page.locator('.show-tag');
    await expect(heroTag).toBeVisible({ timeout: 10000 });
    await expect(heroTag).toContainText('covered calls');
    await expect(heroTag).toContainText('cash-secured puts');
    await expect(heroTag).toContainText('vertical');

    // 3b — 6 fact chips using .show-fact elements
    const facts = page.locator('.show-fact');
    await expect(facts).toHaveCount(6, { timeout: 8000 });

    // Spot-check key chip values
    await expect(page.locator('.show-fact-val').filter({ hasText: /~?70k/i }).first()).toBeVisible();
    await expect(page.locator('.show-fact-val').filter({ hasText: /^5$/ }).first()).toBeVisible();
    await expect(page.locator('.show-fact-val').filter({ hasText: /24.7|24×7/i }).first()).toBeVisible();

    // 3c — 6 architecture cards
    const cards = page.locator('.show-card');
    await expect(cards).toHaveCount(6, { timeout: 8000 });

    await page.screenshot({ path: `${SCREENSHOT_DIR}/showcase.png`, fullPage: true });
  });

  test('3.1 · /showcase contact buttons border visible', async ({ page }) => {
    // Test 1 — Bio buttons border on /showcase page
    // Verify .show-contact-btn elements have visible border (not transparent)
    // Use DEV_URL (localhost) instead of PROD_URL for local testing
    const URL = process.env.PLAYWRIGHT_BASE_URL ? DEV_URL : 'http://localhost:5174';
    await page.goto(URL + '/showcase', { waitUntil: 'domcontentloaded' });

    // Wait for contact buttons to be visible
    const contactButtons = page.locator('.show-contact-btn');

    // Wait for at least 5 buttons to be present (Email, GitHub, LinkedIn, Portfolio, Resume)
    await expect(async () => {
      const count = await contactButtons.count();
      expect(count).toBeGreaterThanOrEqual(5);
    }).toPass({ timeout: 10000 });

    const buttonCount = await contactButtons.count();
    expect(buttonCount).toBeGreaterThanOrEqual(5);

    // Check the first button's computed border-color
    const firstButton = contactButtons.first();
    await expect(firstButton).toBeVisible({ timeout: 10000 });

    const borderColor = await firstButton.evaluate(el => getComputedStyle(el).borderColor);

    // Verify border-color is not transparent or rgba(0,0,0,0)
    expect(borderColor).toBeTruthy();
    expect(borderColor).not.toMatch(/rgba\s*\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\)/);
    expect(borderColor.toLowerCase()).not.toBe('transparent');
  });

  test('4 · /admin/tokens in demo (prod) — read-only banner, no write buttons, tab strip renders', async ({ page }) => {
    page.on('console', msg => msg.type() === 'error' && consoleErrors.push({ url: page.url(), text: msg.text() }));
    page.on('pageerror', err => consoleErrors.push({ url: page.url(), text: `UNCAUGHT: ${err.message}` }));

    // Anonymous visit to prod /admin/tokens → demo mode: read-only
    await page.goto(PROD_URL + '/admin/tokens', { waitUntil: 'domcontentloaded' });
    // Wait for paperStatus poll to confirm branch='main' and isDemo=true
    await page.waitForTimeout(3000);

    // 4a — "Read-only in demo." text (from the InfoHint banner)
    await expect(page.getByText(/Read-only in demo/i).first()).toBeVisible({ timeout: 10000 });

    // 4b — No "+ New token" button (write action hidden in demo)
    await expect(page.getByRole('button', { name: /New token|\+ New/i })).not.toBeVisible();

    // 4c — No "Reload registry" button in demo
    await expect(page.getByRole('button', { name: /Reload registry/i })).not.toBeVisible();

    // 4d — Condition / Notify / Action tab strip renders
    await expect(page.getByText('Condition', { exact: false }).first()).toBeVisible({ timeout: 8000 });
    await expect(page.getByText('Notify', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('Action', { exact: false }).first()).toBeVisible();

    await page.screenshot({ path: `${SCREENSHOT_DIR}/tokens-demo.png`, fullPage: false });
  });

  // ── CHECK 5 — /performance strategy strip (dev.ramboq.com) ─────────

  test('5 · /performance — strategy strip, position/holdings grids render', async ({ page }) => {
    page.on('console', msg => msg.type() === 'error' && consoleErrors.push({ url: page.url(), text: msg.text() }));
    page.on('pageerror', err => consoleErrors.push({ url: page.url(), text: `UNCAUGHT: ${err.message}` }));

    // /performance is public — no auth needed on dev
    await page.goto(DEV_URL + '/performance', { waitUntil: 'domcontentloaded' });

    // 5a — Strategy strip text
    await expect(
      page.getByText(/Long stocks.*algo-executed options overlay/i).or(
        page.getByText(/algo-executed options overlay/i)
      ).first()
    ).toBeVisible({ timeout: 20000 });

    // 5b — ag-Grid root renders (holdings or positions grid)
    const grid = page.locator('.ag-root, .ag-root-wrapper').first();
    await expect(grid).toBeVisible({ timeout: 20000 });

    await page.screenshot({ path: `${SCREENSHOT_DIR}/performance.png`, fullPage: false });
  });

  // ── CHECK 6 — Console error summary ─────────────────────────────────

  test('6 · Console error summary (cross-page)', async ({ page }) => {
    page.on('console', msg => msg.type() === 'error' && consoleErrors.push({ url: page.url(), text: msg.text() }));
    page.on('pageerror', err => consoleErrors.push({ url: page.url(), text: `UNCAUGHT: ${err.message}` }));

    // Visit public pages and collect errors
    for (const url of [DEV_URL + '/', DEV_URL + '/performance', PROD_URL + '/dashboard']) {
      await page.goto(url, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2000);
    }

    // Filter expected 401/403/favicon from demo/dev context
    const meaningful = consoleErrors.filter(e =>
      !/(401|403|favicon|Unauthorized|Demo mode|net::ERR_ABORTED|is_enabled)/i.test(e.text)
    );

    if (meaningful.length > 0) {
      console.warn('Unexpected console errors:', JSON.stringify(meaningful, null, 2));
    }
    expect(meaningful.length, `Unexpected console errors:\n${JSON.stringify(meaningful, null, 2)}`).toBe(0);
  });
});
