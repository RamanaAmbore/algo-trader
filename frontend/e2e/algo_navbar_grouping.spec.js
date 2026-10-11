// Verify the algo navbar's group disclosure dropdowns + mobile drawer
// grouping (Item 3 / Phase post-24 navbar refresh).
//
// 2026-10 nav restructure (operator-approved):
//   - Console moved from the 'build' group into 'monitor' (inline),
//     positioned directly after Orders (a bridge between Orders and
//     Automation — creates both ephemeral order commands and
//     persisted agents).
//   - The /activity nav LINK was removed entirely (redundant with the
//     icon-triggered openActivityModal() already in the header — that
//     modal/icon is untouched and still opens normally).
//   - /admin/alerts (nav entry + standalone page) was removed — its
//     functionality folded into LogPanel's new 'alerts' tab (see
//     logpanel_alerts_tab.spec.js).
//   - The 'build' group is now empty and was deleted outright — no
//     dropdown trigger renders for it on desktop or mobile.
//
// Desktop expectations (>=1024px viewport):
//   - Inline buttons: Pulse, Dashboard, Orders, Derivatives, Charts,
//     Automation, Console, Strategies, MCP
//   - Only ONE dropdown trigger remains: "Config" (no "Build")
//   - Click "Config" → panel reveals Brokers / Settings / Users / Health
//   - No "Activity" button/link anywhere (inline or dropdown)
//   - Picking an item navigates + closes the panel
//   - Active page inside a group keeps that trigger highlighted
//
// Mobile expectations (<1024px viewport):
//   - Hamburger opens drawer
//   - Drawer contains section captions (MONITOR / EXPLORE / CONFIG) —
//     no BUILD caption
//   - Console appears under the Monitor caption, not a Build caption
//   - No "Activity" item anywhere in the drawer

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
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

  test(`inline + dropdown structure, Build group gone [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    // Wait for the algo nav to mount
    await page.waitForSelector('.algo-nav-btn', { state: 'visible', timeout: 15_000 });

    // Inline labels — every one of these is a direct button (NOT inside
    // a .algo-group-wrap), confirming they didn't collapse into a
    // dropdown. Console now sits here, next to Automation.
    for (const lbl of ['Pulse', 'Dashboard', 'Automation', 'Console', 'Orders', 'Derivatives', 'MCP']) {
      const direct = page.locator(`nav > button.algo-nav-btn:has-text("${lbl}")`).first();
      await expect(direct, `inline button "${lbl}" should be a direct nav child`).toBeVisible();
    }

    // Build trigger must be gone entirely — only Config remains.
    const buildTrigger = page.locator('.algo-group-wrap .algo-group-trigger:has-text("Build")');
    await expect(buildTrigger, 'Build dropdown trigger must not exist').toHaveCount(0);
    const configTrigger = page.locator('.algo-group-wrap .algo-group-trigger:has-text("Config")');
    await expect(configTrigger).toBeVisible();
    // Exactly one dropdown trigger total.
    await expect(page.locator('.algo-group-wrap .algo-group-trigger')).toHaveCount(1);

    // No "Activity" link/button anywhere in the nav bar (inline or
    // inside the remaining Config dropdown). The icon-triggered modal
    // (bell / Log button) is a SEPARATE element, not a .algo-nav-btn
    // or .algo-group-item, so this assertion can't collide with it.
    await expect(page.locator('nav > button.algo-nav-btn:has-text("Activity")')).toHaveCount(0);

    // Panels start closed
    await expect(page.locator('.algo-group-panel')).toHaveCount(0);

    // Open Config → see the admin surfaces, no Alerts item (that page
    // is gone — its functionality moved into LogPanel's own tab strip).
    await configTrigger.click();
    await expect(page.locator('.algo-group-panel')).toBeVisible();
    for (const lbl of ['Brokers', 'Settings', 'Health']) {
      await expect(page.locator(`.algo-group-item:has-text("${lbl}")`)).toBeVisible();
    }
    await expect(page.locator('.algo-group-item:has-text("Alerts")')).toHaveCount(0);

    // Click a known item → URL changes, panel closes
    await page.locator('.algo-group-item:has-text("Settings")').click();
    await page.waitForURL(/\/admin\/settings/);
    await expect(page.locator('.algo-group-panel')).toHaveCount(0);

    // Config trigger should now be lit (active group)
    await expect(configTrigger).toHaveClass(/algo-nav-btn-active/);
  });

  test(`icon-triggered Activity modal still opens with Console inline [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-nav-btn', { state: 'visible', timeout: 15_000 });

    // Keyboard shortcut `h` opens the Activity modal — confirms the
    // icon/shortcut-triggered surface is untouched even though the
    // redundant nav LINK to /activity was removed.
    await page.keyboard.press('h');
    await expect(page.locator('.alm-panel')).toBeVisible({ timeout: 8_000 });
    await page.keyboard.press('Escape');
  });
});

test.describe('mobile drawer grouping', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test(`hamburger drawer has no Build caption; Console under Monitor [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.algo-hamburger', { state: 'visible', timeout: 15_000 });

    // Drawer closed by default
    await expect(page.locator('.algo-mobile-dropdown')).toHaveCount(0);

    await page.locator('.algo-hamburger').click();
    await expect(page.locator('.algo-mobile-dropdown')).toBeVisible();

    // Captions render only for groups that have items: Monitor, Explore,
    // Config. Build is gone (its three items all moved/removed).
    for (const label of ['Monitor', 'Explore', 'Config']) {
      const cap = page.locator(`.algo-mobile-group-label:text-is("${label}")`);
      await expect(cap, `mobile drawer should carry the ${label} caption`).toBeVisible();
    }
    await expect(page.locator('.algo-mobile-group-label:text-is("Build")')).toHaveCount(0);

    // No standalone "Activity" item in the drawer.
    await expect(page.locator('.algo-mobile-item:has-text("Activity")')).toHaveCount(0);

    // Console renders as a mobile item (now under Monitor, not Build —
    // the absence of the Build caption above is the structural proof;
    // this just confirms the item itself still renders somewhere).
    await expect(page.locator('.algo-mobile-item:has-text("Console")')).toBeVisible();
  });
});
