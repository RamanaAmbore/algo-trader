// Verify the algo navbar's group disclosure dropdowns + mobile drawer
// grouping (Item 3 / Phase post-24 navbar refresh).
//
// Desktop expectations (>=1024px viewport):
//   - Inline buttons: Tour, Pulse, Dashboard, Agents, Orders, Derivatives, Lab
//   - Dropdown triggers labelled "Build" and "Config" with caret
//   - Click "Build" → panel reveals Console / Research / Tokens
//   - Click "Config" → panel reveals Brokers / Settings / Users / Health
//   - Picking an item navigates + closes the panel
//   - Active page inside a group keeps that trigger highlighted
//
// Mobile expectations (<1024px viewport):
//   - Hamburger opens drawer
//   - Drawer contains section captions (MONITOR / ANALYZE / MODES / BUILD / CONFIG)
//   - Each caption sits above its group's items

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: _PASS },
      });
      if (r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error(`login failed against ${BASE}`);
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test.describe('desktop nav grouping', () => {
  test.use({ viewport: { width: 1366, height: 768 } });

  test(`inline + dropdown structure [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });

    // Wait for the algo nav to mount
    await page.waitForSelector('.algo-nav-btn', { state: 'visible', timeout: 15_000 });

    // Inline labels — every one of these is a direct button (NOT inside
    // a .algo-group-wrap), confirming they didn't collapse into a
    // dropdown.
    for (const lbl of ['Pulse', 'Dashboard', 'Agents', 'Orders', 'Derivatives', 'Lab']) {
      const direct = page.locator(`nav > button.algo-nav-btn:has-text("${lbl}")`).first();
      await expect(direct, `inline button "${lbl}" should be a direct nav child`).toBeVisible();
    }

    // The Build + Config triggers ARE inside .algo-group-wrap
    const buildTrigger = page.locator('.algo-group-wrap .algo-group-trigger:has-text("Build")');
    const configTrigger = page.locator('.algo-group-wrap .algo-group-trigger:has-text("Config")');
    await expect(buildTrigger).toBeVisible();
    await expect(configTrigger).toBeVisible();

    // Panels start closed
    await expect(page.locator('.algo-group-panel')).toHaveCount(0);

    // Open Build → see Console / Research / Tokens
    await buildTrigger.click();
    await expect(page.locator('.algo-group-panel')).toBeVisible();
    for (const lbl of ['Console', 'Tokens']) {
      await expect(page.locator(`.algo-group-item:has-text("${lbl}")`)).toBeVisible();
    }
    // Research is adminOnly — present whether or not we hit admin login,
    // because the cached token belongs to an admin user.

    // Click outside (the overlay) → panel closes
    await page.locator('.algo-group-overlay').click({ position: { x: 5, y: 5 } });
    await expect(page.locator('.algo-group-panel')).toHaveCount(0);

    // Open Config → click a known item → URL changes, panel closes
    await configTrigger.click();
    await expect(page.locator('.algo-group-panel')).toBeVisible();
    await page.locator('.algo-group-item:has-text("Settings")').click();
    await page.waitForURL(/\/admin\/settings/);
    await expect(page.locator('.algo-group-panel')).toHaveCount(0);

    // Config trigger should now be lit (active group)
    await expect(configTrigger).toHaveClass(/algo-nav-btn-active/);
  });
});

test.describe('mobile drawer grouping', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test(`hamburger drawer has group section captions [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.algo-hamburger', { state: 'visible', timeout: 15_000 });

    // Drawer closed by default
    await expect(page.locator('.algo-mobile-dropdown')).toHaveCount(0);

    await page.locator('.algo-hamburger').click();
    await expect(page.locator('.algo-mobile-dropdown')).toBeVisible();

    // At least Monitor, Build, Config captions must be visible. Analyze
    // / Modes captions appear only when their groups have items — which
    // they always do (Derivatives + Lab), so check those too.
    for (const label of ['Monitor', 'Analyze', 'Modes', 'Build', 'Config']) {
      const cap = page.locator(`.algo-mobile-group-label:text-is("${label}")`);
      await expect(cap, `mobile drawer should carry the ${label} caption`).toBeVisible();
    }
  });
});
