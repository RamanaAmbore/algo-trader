// Visual diagnostic — click the Show + Account triggers and capture
// what's actually visible on screen. If the dropdowns aren't opening
// at all in user's browser, this confirms vs disproves it.

import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse filter visual check [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(4500);

  const slug = BASE.includes('dev') ? 'dev' : 'prod';

  // Click the Show trigger
  await page.locator('.mp-chrome-row > div.w-44 button').first().click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `test-results/fv-${slug}-show-open.png`, clip: { x: 0, y: 50, width: 700, height: 600 } });

  // Is the panel visible?
  const showPanel = page.locator('.rbq-multi-panel').first();
  const showPanelVisible = await showPanel.isVisible().catch(() => false);
  const showPanelBox = showPanelVisible ? await showPanel.boundingBox() : null;
  console.log(`Show panel visible: ${showPanelVisible}, box: ${JSON.stringify(showPanelBox)}`);

  // Get its computed style
  if (showPanelVisible) {
    const cs = await showPanel.evaluate((el) => {
      const s = window.getComputedStyle(el);
      return {
        display: s.display, visibility: s.visibility, opacity: s.opacity,
        zIndex: s.zIndex, position: s.position,
        width: s.width, height: s.height,
        backgroundColor: s.backgroundColor,
      };
    });
    console.log(`Show panel computed style: ${JSON.stringify(cs)}`);
    const optionCount = await showPanel.locator('li[role="option"]').count();
    console.log(`Show panel option count: ${optionCount}`);
  }

  // Close + open account picker
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  await page.locator('.mp-chrome-row > div.w-28 button').first().click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `test-results/fv-${slug}-acct-open.png`, clip: { x: 0, y: 50, width: 700, height: 600 } });

  const acctPanel = page.locator('.rbq-multi-panel').first();
  const acctVisible = await acctPanel.isVisible().catch(() => false);
  console.log(`Account panel visible: ${acctVisible}`);
  if (acctVisible) {
    const optionCount = await acctPanel.locator('li[role="option"]').count();
    console.log(`Account panel option count: ${optionCount}`);
  }

  // Try clicking the × clear button on Show
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  const showClear = page.locator('.mp-chrome-row > div.w-44 button.rbq-multi-clear');
  if (await showClear.isVisible().catch(() => false)) {
    console.log('Show × clear button visible — clicking');
    await showClear.click();
    await page.waitForTimeout(500);
    const showTriggerText = (await page.locator('.mp-chrome-row > div.w-44 button.rbq-multi-trigger').first().textContent() ?? '').trim();
    console.log(`Show trigger after clear: "${showTriggerText}"`);
    await page.screenshot({ path: `test-results/fv-${slug}-show-cleared.png`, clip: { x: 0, y: 50, width: 700, height: 200 } });

    // Now try opening it back up
    await page.locator('.mp-chrome-row > div.w-44 button.rbq-multi-trigger').first().click();
    await page.waitForTimeout(400);
    const showPanelAgain = page.locator('.rbq-multi-panel').first();
    const visAgain = await showPanelAgain.isVisible().catch(() => false);
    console.log(`After clear → click: panel visible: ${visAgain}`);
    if (visAgain) {
      const optCount = await showPanelAgain.locator('li[role="option"]').count();
      console.log(`Options listed: ${optCount}`);
    }
    await page.screenshot({ path: `test-results/fv-${slug}-show-after-clear-reopen.png`, clip: { x: 0, y: 50, width: 700, height: 600 } });
  } else {
    console.log('Show × clear button NOT visible (no selection or hidden)');
  }
});
