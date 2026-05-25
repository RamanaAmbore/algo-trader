import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse debug movers state [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
    sessionStorage.removeItem('mp.selectedAccounts');
    sessionStorage.removeItem('mp.selectedShow');
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(15000);

  const dbg = await page.evaluate(() => /** @type {any} */ (window).__unifiedDebug ?? null);
  console.log(`__unifiedDebug: ${JSON.stringify(dbg, null, 2)}`);
});
