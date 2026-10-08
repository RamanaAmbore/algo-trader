// Verify the AutomationTabs strip on the Automation workspace surfaces.
// The strip shows exactly three tabs — Agents, Brackets, Agent
// Templates — and the one matching the current route carries the active
// state (AlgoTabs: role=tab + aria-selected="true").
//
// Agent fire history is no longer a tab here; /automation/activity
// redirects to /activity?tab=agent (covered by the last test).

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

const TABS = [
  { href: '/automation',                 label: 'Agents'          },
  { href: '/automation/templates',       label: 'Brackets' },
  { href: '/automation/agent-templates', label: 'Agent Templates' },
];

const STRIP = '.aw-tabs-wrap [role="tab"]';

test.describe('automation workspace tabs', () => {
  test.use({ viewport: { width: 1366, height: 768 } });

  for (const surface of TABS) {
    test(`strip shows exactly 3 tabs on ${surface.href} with ${surface.label} active [${BASE}]`, async ({ page }) => {
      await login(page);
      await page.goto(`${BASE}${surface.href}`, { waitUntil: 'networkidle' });
      await page.waitForSelector('.aw-tabs-wrap [role="tablist"]', { state: 'visible', timeout: 15_000 });

      const tabs = page.locator(STRIP);
      await expect(tabs).toHaveCount(3);
      await expect(tabs).toHaveText(TABS.map((t) => t.label));

      const active = page.locator(`${STRIP}[aria-selected="true"]`);
      await expect(active).toHaveCount(1);
      await expect(active).toHaveText(surface.label);
    });
  }

  test(`click navigates between tabs [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/automation`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.aw-tabs-wrap [role="tablist"]', { state: 'visible', timeout: 15_000 });

    for (const t of TABS.slice(1)) {
      await page.locator(STRIP, { hasText: t.label }).click();
      await page.waitForURL(new RegExp(t.href.replace(/\//g, '\\/') + '$'));
      await expect(page.locator(`${STRIP}[aria-selected="true"]`)).toHaveText(t.label);
    }
  });

  test(`/automation/activity redirects to /activity?tab=agent [${BASE}]`, async ({ page }) => {
    await login(page);
    await page.goto(`${BASE}/automation/activity`, { waitUntil: 'networkidle' });
    await page.waitForURL(/\/activity\?.*tab=agent/);
    expect(new URL(page.url()).pathname).toBe('/activity');
  });
});
