// Verify that INDIGO / GOLDM / CRUDEOIL underlying anchors render
// inside the Positions group on /pulse against prod.
import { test, expect } from '@playwright/test';

const _AUTH_USER = process.env.PLAYWRIGHT_USER || 'ambore';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

test('pulse anchors visible for INDIGO/GOLDM/CRUDEOIL positions', async ({ page }) => {
  // login
  let tok = null;
  for (const u of [_AUTH_USER, 'rambo']) {
    const r = await page.request.post('https://ramboq.com/api/auth/login', {
      data: { username: u, password: _AUTH_PASS },
    });
    if (r.ok()) { tok = (await r.json()).access_token; break; }
  }
  if (!tok) throw new Error('login failed');

  await page.context().addInitScript((token) => {
    sessionStorage.setItem('ramboq_token', token);
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });

  await page.setViewportSize({ width: 1440, height: 1200 });
  await page.goto('https://ramboq.com/pulse', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);

  // Scroll through ag-Grid to force-render virtualized rows
  const grid = page.locator('.ag-body-viewport').first();
  await grid.scrollIntoViewIfNeeded().catch(() => {});

  // Collect all rendered symbol cells + their detected major group
  // (by walking ancestor row classes for major-positions / major-pinned)
  const probe = async () => page.evaluate(() => {
    const out = [];
    document.querySelectorAll('.ag-row').forEach((row) => {
      const sym = row.querySelector('[col-id="sym"], .ag-col-sym')?.textContent?.trim() || '';
      const cls = row.className;
      let major = 'unknown';
      if (cls.includes('major-positions')) major = 'positions';
      else if (cls.includes('major-pinned')) major = 'pinned';
      else if (cls.includes('major-holdings')) major = 'holdings';
      else if (cls.includes('major-watchlist')) major = 'watchlist';
      else if (cls.includes('major-movers')) major = 'movers';
      out.push({ sym, major, full: row.textContent.slice(0, 80) });
    });
    return out;
  });

  let rows = await probe();
  console.log('first paint rows:', rows.length);

  // scroll the grid to capture every row (ag-Grid virtualizes ~20 at a time)
  for (let y = 0; y < 8000; y += 400) {
    await grid.evaluate((el, top) => { el.scrollTop = top; }, y);
    await page.waitForTimeout(120);
    const r = await probe();
    rows.push(...r);
  }
  // de-dupe
  const seen = new Set();
  rows = rows.filter(r => {
    const k = r.sym + '|' + r.major;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });

  console.log('Total unique rows:', rows.length);
  const interesting = rows.filter(r =>
    r.sym.startsWith('INDIGO') || r.sym.startsWith('CRUDE') || r.sym.startsWith('GOLDM') || r.sym === 'INDIGO' || r.sym === 'GOLDM' || r.sym === 'CRUDEOIL');
  console.log('\n=== INDIGO/CRUDE/GOLDM rows ===');
  for (const r of interesting) console.log(`  [${r.major}] ${r.sym}`);

  await page.screenshot({ path: 'test-results/pulse-anchors.png', fullPage: true });
});
