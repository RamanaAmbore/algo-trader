import { test } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test(`pulse current state diag [${BASE}]`, async ({ page }) => {
  let tok = null;
  for (const u of ['ambore', 'rambo']) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
    sessionStorage.removeItem('mp.selectedAccounts');
    sessionStorage.removeItem('mp.selectedShow');
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  await page.setViewportSize({ width: 1440, height: 1000 });

  // Capture network failures during page load
  const failures = [];
  page.on('response', (resp) => {
    if (resp.status() >= 500) {
      failures.push({ url: resp.url(), status: resp.status() });
    }
  });

  await page.goto(`${BASE}/pulse`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(6000);

  console.log(`5xx failures during load: ${JSON.stringify(failures)}`);

  // Account picker visibility
  const acctPickerWrapper = page.locator('.mp-chrome-row > div.w-28');
  const acctVisible = await acctPickerWrapper.first().isVisible().catch(() => false);
  console.log(`account picker wrapper visible: ${acctVisible}`);

  if (acctVisible) {
    const acctTrigger = acctPickerWrapper.locator('button.rbq-multi-trigger').first();
    const acctText = (await acctTrigger.textContent() ?? '').trim();
    console.log(`account trigger: "${acctText}"`);
  }

  // Show picker
  const showTrigger = page.locator('.mp-chrome-row > div.w-44 button.rbq-multi-trigger').first();
  const showText = (await showTrigger.textContent() ?? '').trim();
  console.log(`show trigger: "${showText}"`);

  // Count rows
  const totalRows = await page.locator('.ag-row').count();
  const posRows = await page.locator('.ag-row.pos-long, .ag-row.pos-short, .ag-row.row-pos').count();
  const holdRows = await page.locator('.ag-row.row-hold').count();
  const watchRows = await page.locator('.ag-row.row-watch').count();
  console.log(`rows: total=${totalRows} pos=${posRows} hold=${holdRows} watch=${watchRows}`);

  await page.screenshot({ path: 'test-results/diag-pulse.png', fullPage: false });
  await page.screenshot({ path: 'test-results/diag-pulse-full.png', fullPage: true });
});
