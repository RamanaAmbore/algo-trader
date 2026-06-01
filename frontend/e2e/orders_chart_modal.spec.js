/**
 * Test: open SymbolPanel from a pulse row (Place order context menu)
 * → click chart icon inside SymbolPanel header
 * → ChartModal opens
 * → click X → must close.
 */
import { test } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => sessionStorage.setItem('ramboq_token', t), _cachedToken);
}

test('SymbolPanel → chart icon → ChartModal X close', async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);

  const log = [];
  page.on('pageerror', async (e) => {
    log.push(`[pageerror] ${e.message}`);
    try {
      const probe = await page.evaluate(() => ({
        ssLastQ: window.__ssLast ? `query="${window.__ssLastQ}"` : '<not-fired>',
        ssLastLen: window.__ssLast?.length ?? 0,
        ssLastDups: (() => {
          if (!window.__ssLast) return [];
          const keys = window.__ssLast.map(i => `${i.sym}:${i.e ?? ''}:${i.t ?? ''}`);
          const cnt = {};
          keys.forEach(k => { cnt[k] = (cnt[k] || 0) + 1; });
          return Object.entries(cnt).filter(([_, v]) => v > 1).slice(0, 5);
        })(),
      }));
      log.push(`SS probe at error: ${JSON.stringify(probe)}`);
    } catch (_) { /* page closed */ }
  });
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') log.push(`[${m.type()}] ${m.text().slice(0, 600)}`);
  });

  await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });
  // Retry-wait for NIFTY row to render
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(2500);
    if ((await page.locator('.ag-row').filter({ hasText: 'NIFTY' }).count()) > 0) break;
  }
  const niftyRow = page.locator('.ag-row').filter({ hasText: 'NIFTY' }).first();
  log.push(`NIFTY rows: ${await niftyRow.count()}`);

  // Open context menu via sym-actions click
  await niftyRow.locator('.sym-actions').first().evaluate((el) => {
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
  });
  await page.waitForTimeout(500);
  // Click "Place order →"
  const placeOrder = page.locator('button.ctx-item:has-text("Place order")').first();
  log.push(`Place order menu items: ${await placeOrder.count()}`);
  if (await placeOrder.count() === 0) {
    log.push('FAIL: Place order context item missing');
    console.log(log.join('\n'));
    return;
  }
  await placeOrder.click();
  await page.waitForTimeout(2000);

  const symPanel = page.locator('.oes-overlay');
  log.push(`SymbolPanel .oes-overlay count: ${await symPanel.count()}`);
  if (await symPanel.count() === 0) {
    log.push('FAIL: SymbolPanel did not open');
    console.log(log.join('\n'));
    return;
  }
  log.push('✓ SymbolPanel opened');

  // Find the .oes-chart-btn (chart icon)
  const chartIcon = page.locator('.oes-chart-btn').first();
  const chartIconCount = await chartIcon.count();
  log.push(`.oes-chart-btn count: ${chartIconCount}`);
  if (chartIconCount === 0) {
    log.push('FAIL: chart icon not present in SymbolPanel');
    console.log(log.join('\n'));
    return;
  }
  const chartIconDisabled = await chartIcon.evaluate((el) => /** @type {HTMLButtonElement} */ (el).disabled);
  log.push(`chart icon disabled: ${chartIconDisabled}`);

  await chartIcon.click({ force: true });
  await page.waitForTimeout(2500);

  const chartModal = page.locator('.cm-overlay');
  const chartModalCount = await chartModal.count();
  log.push(`.cm-overlay count after chart click: ${chartModalCount}`);

  if (chartModalCount === 0) {
    log.push('FAIL: ChartModal did not open from SymbolPanel');
    await page.screenshot({ path: 'test-results/sympanel-no-chart.png', fullPage: true });
    console.log(log.join('\n'));
    return;
  }
  log.push('✓ ChartModal opened from SymbolPanel');

  // Now click X
  const x = page.locator('.cm-close').first();
  await x.click({ force: false }).catch(e => log.push(`X click err: ${e.message}`));
  await page.waitForTimeout(900);

  const afterX = await chartModal.count();
  log.push(`After X — .cm-overlay count: ${afterX}`);
  if (afterX === 0) {
    log.push('✓ X closed ChartModal (nested context)');
  } else {
    log.push('❌ BUG CONFIRMED: X did NOT close ChartModal when opened from SymbolPanel');
    await page.screenshot({ path: 'test-results/sympanel-x-fail.png', fullPage: true });
  }
  log.push(`SymbolPanel still open underneath: ${await symPanel.count() > 0}`);

  console.log(log.join('\n'));
});
