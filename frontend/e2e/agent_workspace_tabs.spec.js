// Verify the AgentWorkspaceTabs strip appears on every agent-related
// surface and that clicking each tab lands on the right URL with the
// correct tab lit.

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

test.describe.configure({ mode: 'serial' });

const TAB_ROUTES = [
  { href: '/agents',          label: 'Agents'   },
  { href: '/agents/activity', label: 'Activity' },
  { href: '/admin/tokens',    label: 'Tokens'   },
  { href: '/admin/research',  label: 'Lab'      },
];

test.describe('agent workspace tabs', () => {
  test.use({ viewport: { width: 1366, height: 768 } });

  for (const surface of TAB_ROUTES) {
    test(`strip renders on ${surface.href} with ${surface.label} lit [${BASE}]`, async ({ page }) => {
      await login(page);
      await page.goto(`${BASE}${surface.href}`, { waitUntil: 'networkidle' });
      await page.waitForSelector('.aw-tabs', { state: 'visible', timeout: 15_000 });

      // All four tabs visible
      for (const t of TAB_ROUTES) {
        await expect(page.locator(`.aw-tab:text-is("${t.label}")`)).toBeVisible();
      }

      // Only the current surface's tab carries the active class
      const activeTab = page.locator(`.aw-tab-active:text-is("${surface.label}")`);
      await expect(activeTab).toBeVisible();
    });
  }

  test(`click navigates between tabs [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/agents`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.aw-tabs', { state: 'visible', timeout: 15_000 });

    // Click each tab and assert URL lands correctly.
    for (const t of TAB_ROUTES.slice(1)) {
      await page.locator(`.aw-tab:text-is("${t.label}")`).click();
      await page.waitForURL(new RegExp(t.href.replace(/\//g, '\\/')));
      const active = page.locator(`.aw-tab-active:text-is("${t.label}")`);
      await expect(active).toBeVisible();
    }
  });
});
