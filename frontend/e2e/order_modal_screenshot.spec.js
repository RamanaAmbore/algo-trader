/**
 * One-shot screenshot of /pulse → Place order → SymbolPanel
 * to identify the duplicate symbol border the user reports.
 */
import { test } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;
async function login(page) {
  for (const u of ['ambore', 'rambo', 'admin']) {
    const r = await page.request.post(`${API_HOST}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS }, timeout: 15_000,
    }).catch(() => null);
    if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
  }
  await page.context().addInitScript((t) => sessionStorage.setItem('ramboq_token', t), _cachedToken);
}

test('snap order modal', async ({ page }) => {
  test.setTimeout(90_000);
  await login(page);
  await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(2500);
    if ((await page.locator('.ag-row').filter({ hasText: 'NIFTY' }).count()) > 0) break;
  }
  const niftyRow = page.locator('.ag-row').filter({ hasText: 'NIFTY' }).first();
  await niftyRow.locator('.sym-actions').first().evaluate((el) => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  });
  await page.waitForTimeout(500);
  await page.locator('button.ctx-item:has-text("Place order")').first().click();
  await page.waitForTimeout(2500);
  await page.screenshot({ path: 'test-results/order-modal.png', fullPage: false, clip: { x: 0, y: 0, width: 1400, height: 350 } });
  // Probe: list all elements with border styling near the symbol input
  const probe = await page.evaluate(() => {
    const input = document.querySelector('.oes-sym-input');
    if (!input) return { error: 'no input' };
    const chain = [];
    let cur = input;
    while (cur && cur.tagName !== 'BODY') {
      const cs = window.getComputedStyle(cur);
      const border = cs.border;
      const outline = cs.outline;
      const bs = cs.boxShadow;
      if (border !== '0px none rgb(0, 0, 0)' || outline !== 'rgb(0, 0, 0) none 0px' || bs !== 'none') {
        chain.push({
          tag: cur.tagName,
          cls: typeof cur.className === 'string' ? cur.className.slice(0, 80) : '<svg>',
          border, outline, boxShadow: bs,
        });
      }
      cur = cur.parentElement;
    }
    return { chain };
  });
  console.log('=== BORDER CHAIN around .oes-sym-input ===');
  console.log(JSON.stringify(probe, null, 2));
});
